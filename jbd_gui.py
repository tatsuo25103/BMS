from __future__ import annotations

import csv
import queue
import shutil
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import serial

from jbd_hv_protocol import BmsSample

from jbd_collector import (
    CsvLogger,
    DEFAULT_BAUD_RATES,
    JbdProtocolError,
    PRODUCT_HV140,
    PRODUCT_PS5120E,
    SUPPORTED_PRODUCT_MODELS,
    available_ports,
    build_log_path,
    charge_discharge_state,
    poll_bms,
    sample_error_codes,
    scan_bms,
)

PRODUCT_MODELS = SUPPORTED_PRODUCT_MODELS
PRODUCT_PROTOCOL_LABELS = {
    PRODUCT_HV140: "JBD-HV",
    PRODUCT_PS5120E: "PACE RS232",
}
PRODUCT_MODEL_MAX_PACKS = {
    PRODUCT_HV140: 14,
    PRODUCT_PS5120E: 30,
}
MAX_CELL_PACKS = max(PRODUCT_MODEL_MAX_PACKS.values())


CHART_BG = "#050505"
CHART_LIVE_SAMPLE_LIMIT = 3600
PANEL_BG = "#0b0b0b"
TEXT_FG = "#e6e6e6"
MUTED_FG = "#9ca3af"
GRID_FG = "#262626"
ORANGE = "#ff7a1a"
GREEN = "#22c55e"
BLUE = "#4f46e5"
CYAN = "#22d3ee"
RED = "#ef4444"
PURPLE = "#a855f7"
CHART_LEFT_MARGIN = 54
CHART_RIGHT_MARGIN = 54


class TimeSeriesChart(ttk.Frame):
    def __init__(
        self,
        master: tk.Widget,
        *,
        title: str,
        border_color: str,
        height: int,
        dual_axis: bool = False,
    ) -> None:
        super().__init__(master)
        self.title = title
        self.border_color = border_color
        self.dual_axis = dual_axis
        self.configure(style="Dark.TFrame")
        self.canvas = tk.Canvas(
            self,
            height=height,
            background=CHART_BG,
            highlightthickness=3,
            highlightbackground=border_color,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())
        self.series: list[tuple[str, list[float | None], str, str]] = []
        self.x_labels: list[str] = []
        self.reference_lines: list[tuple[float, str, str]] = []
        self.event_markers: list[tuple[int, float | None, str, str, str]] = []

    def set_series(
        self,
        series: list[tuple[str, list[float | None], str, str]],
        *,
        x_labels: list[str] | None = None,
        reference_lines: list[tuple[float, str, str]] | None = None,
        event_markers: list[tuple[int, float | None, str, str, str]] | None = None,
    ) -> None:
        self.series = series
        self.x_labels = x_labels or []
        self.reference_lines = reference_lines or []
        self.event_markers = event_markers or []
        self.redraw()

    def redraw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 20)
        height = max(canvas.winfo_height(), 20)
        left = CHART_LEFT_MARGIN
        right = width - CHART_RIGHT_MARGIN
        top = 28
        bottom = height - 42

        canvas.create_text(left, 14, text=self.title, fill=TEXT_FG, anchor="w", font=("Segoe UI", 10, "bold"))
        if not self.series:
            canvas.create_text(width / 2, height / 2, text="No data", fill=MUTED_FG, font=("Segoe UI", 10))
            return

        for index in range(5):
            y = top + (bottom - top) * index / 4
            canvas.create_line(left, y, right, y, fill=GRID_FG)
        canvas.create_line(left, top, left, bottom, fill=MUTED_FG)
        canvas.create_line(left, bottom, right, bottom, fill=MUTED_FG)
        if self.dual_axis:
            canvas.create_line(right, top, right, bottom, fill=MUTED_FG)

        left_values = [
            value
            for _name, values, _color, axis in self.series
            if axis == "left"
            for value in values
            if value is not None
        ]
        if self.reference_lines:
            left_values.extend(value for value, _color, _label in self.reference_lines)
        left_values.extend(
            value
            for _index, value, _color, _label, axis in self.event_markers
            if value is not None and axis == "left"
        )
        right_values = [
            value
            for _name, values, _color, axis in self.series
            if axis == "right"
            for value in values
            if value is not None
        ]

        left_min, left_max = padded_range(left_values)
        right_min, right_max = padded_range(right_values)
        canvas.create_text(8, top, text=format_axis(left_max), fill=MUTED_FG, anchor="w", font=("Segoe UI", 8))
        canvas.create_text(8, bottom, text=format_axis(left_min), fill=MUTED_FG, anchor="w", font=("Segoe UI", 8))
        if self.dual_axis and right_values:
            canvas.create_text(width - 8, top, text=format_axis(right_max), fill=MUTED_FG, anchor="e", font=("Segoe UI", 8))
            canvas.create_text(width - 8, bottom, text=format_axis(right_min), fill=MUTED_FG, anchor="e", font=("Segoe UI", 8))

        for value, color, label in self.reference_lines:
            if left_min <= value <= left_max:
                y = bottom - (bottom - top) * (value - left_min) / (left_max - left_min)
                canvas.create_line(left, y, right, y, fill=color, dash=(5, 4), width=1)
                canvas.create_text(right - 4, y - 8, text=label, fill=color, anchor="e", font=("Segoe UI", 8, "bold"))

        max_len = max((len(values) for _name, values, _color, _axis in self.series), default=0)
        self._draw_x_axis_labels(canvas, left, right, top, bottom, max_len)
        if max_len < 2:
            return

        legend_x = left + 10
        for name, values, color, axis in self.series:
            points: list[tuple[float, float]] = []
            axis_min, axis_max = (right_min, right_max) if axis == "right" else (left_min, left_max)
            for index, value in enumerate(values):
                if value is None:
                    continue
                x = left + (right - left) * index / (max_len - 1)
                y = bottom - (bottom - top) * (value - axis_min) / (axis_max - axis_min)
                points.append((x, y))
            for p1, p2 in zip(points, points[1:]):
                canvas.create_line(p1[0], p1[1], p2[0], p2[1], fill=color, width=2)
            if points:
                canvas.create_oval(points[-1][0] - 2, points[-1][1] - 2, points[-1][0] + 2, points[-1][1] + 2, fill=color, outline=color)
            canvas.create_rectangle(legend_x, height - 18, legend_x + 10, height - 8, fill=color, outline=color)
            canvas.create_text(legend_x + 14, height - 13, text=name, fill=TEXT_FG, anchor="w", font=("Segoe UI", 8))
            legend_x += 78

        self._draw_event_markers(canvas, left, right, top, bottom, max_len, left_min, left_max, right_min, right_max)

    def _draw_event_markers(
        self,
        canvas: tk.Canvas,
        left: int,
        right: int,
        top: int,
        bottom: int,
        max_len: int,
        left_min: float,
        left_max: float,
        right_min: float,
        right_max: float,
    ) -> None:
        if max_len <= 0:
            return
        label_slots: dict[int, int] = {}
        for index, value, color, label, axis in self.event_markers:
            if index < 0 or index >= max_len:
                continue
            x = left if max_len == 1 else left + (right - left) * index / (max_len - 1)
            label_slot = label_slots.get(index, 0)
            label_slots[index] = label_slot + 1
            if value is None:
                y = top + 14 + label_slot * 14
            else:
                axis_min, axis_max = (right_min, right_max) if axis == "right" else (left_min, left_max)
                if axis_max == axis_min:
                    y = (top + bottom) / 2
                else:
                    y = bottom - (bottom - top) * (value - axis_min) / (axis_max - axis_min)
                    y = min(bottom - 8, max(top + 8, y))
                if label_slot:
                    y = min(bottom - 8, y + label_slot * 11)
            canvas.create_line(x, top, x, bottom, fill=color, dash=(2, 5), width=1)
            canvas.create_polygon(x, y - 7, x - 5, y + 3, x + 5, y + 3, fill=color, outline=color)
            canvas.create_text(x + 6, y - 8, text=label, fill=color, anchor="w", font=("Segoe UI", 8, "bold"))

    def _draw_x_axis_labels(
        self,
        canvas: tk.Canvas,
        left: int,
        right: int,
        top: int,
        bottom: int,
        max_len: int,
    ) -> None:
        if not self.x_labels or max_len <= 0:
            return
        plot_width = max(right - left, 1)
        # 以 10 個時間區間為基準，視窗較寬時自動增加刻度但避免過密。
        target_label_count = max(11, min(18, int(plot_width / 95) + 1))
        label_count = min(max_len, target_label_count)
        if label_count <= 1:
            indexes = [0]
        else:
            indexes = sorted(
                {
                    round(index * (max_len - 1) / (label_count - 1))
                    for index in range(label_count)
                }
            )
        for index in indexes:
            if index < 0 or index >= len(self.x_labels):
                continue
            x = left if max_len == 1 else left + (right - left) * index / (max_len - 1)
            canvas.create_line(x, top, x, bottom, fill=GRID_FG)
            canvas.create_line(x, bottom, x, bottom + 4, fill=MUTED_FG)
            canvas.create_text(x, bottom + 16, text=self.x_labels[index], fill=MUTED_FG, anchor="n", font=("Segoe UI", 8))


class BmsCollectorGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.title("JBD-HVBCUM01 BMS Collector")
        self.geometry("1180x720")
        self.minsize(1000, 620)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.running = False
        self.history: list[object] = []
        self.record_events: list[tuple[int, str, str]] = []
        self.start_event_pending = False
        self.packet_ok_count = 0
        self.packet_error_count = 0
        self.consecutive_packet_errors = 0
        self.last_packet_ok_monotonic: float | None = None
        self.link_monitor_started_monotonic: float | None = None
        self.link_stale = False
        self.control_widgets: list[tk.Widget] = []
        self.cell_value_labels: list[tk.Label] = []
        self.pack_value_frames: list[tk.Frame] = []
        self.temp_value_labels: list[tk.Label] = []
        self.temp_value_frames: list[tk.Frame] = []
        self.temp_group_title_labels: list[tk.Label] = []
        self.status_value_labels: dict[str, tk.Label] = {}
        self.logo_image: tk.PhotoImage | None = None
        self.busy_window: tk.Toplevel | None = None
        self.busy_progress: ttk.Progressbar | None = None

        self.port_var = tk.StringVar()
        self.product_model_var = tk.StringVar(value=PRODUCT_HV140)
        self.baud_var = tk.IntVar(value=9600)
        self.max_packs_var = tk.IntVar(value=14)
        self.interval_var = tk.DoubleVar(value=model_minimum_interval_seconds(PRODUCT_HV140, 14))
        self.interval_label_var = tk.StringVar()
        self.timeout_var = tk.DoubleVar(value=3.0)
        self.csv_var = tk.StringVar(value=str(Path.home() / "Downloads" / "bms_log.csv"))
        self.current_log_path: Path | None = None
        self.status_var = tk.StringVar(value="Idle")

        self.value_vars = {
            "connection": tk.StringVar(value="--"),
            "link_status": tk.StringVar(value="Idle"),
            "packet_status": tk.StringVar(value="--"),
            "packet_counts": tk.StringVar(value="RX 0 / ERR 0 / Streak 0"),
            "last_rx": tk.StringVar(value="--"),
            "last_error": tk.StringVar(value="--"),
            "serial": tk.StringVar(value="--"),
            "fw": tk.StringVar(value="--"),
            "voltage": tk.StringVar(value="--"),
            "current": tk.StringVar(value="--"),
            "state": tk.StringVar(value="--"),
            "soc": tk.StringVar(value="--"),
            "packs": tk.StringVar(value="--"),
            "cells": tk.StringVar(value="--"),
            "cell_range": tk.StringVar(value="--"),
            "highest_cell": tk.StringVar(value="--"),
            "lowest_cell": tk.StringVar(value="--"),
            "highest_temp": tk.StringVar(value="--"),
            "lowest_temp": tk.StringVar(value="--"),
            "timestamp": tk.StringVar(value="--"),
            "csv_file": tk.StringVar(value=f"Next: {self.csv_var.get()}"),
        }

        self._build_ui()
        self.product_model_var.trace_add("write", lambda *_args: self._on_product_model_changed())
        self._sync_interval_slider()
        self.refresh_ports()
        self.after(150, self._drain_events)
        self.after(500, self._monitor_link_health)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.configure(background="#000000")
        style = ttk.Style(self)
        style.configure("Dark.TFrame", background="#000000")
        style.configure("Panel.TFrame", background=PANEL_BG)
        style.configure("Panel.TLabel", background=PANEL_BG, foreground=TEXT_FG)
        style.configure("PanelMuted.TLabel", background=PANEL_BG, foreground=MUTED_FG)
        style.configure("Panel.TLabelframe", background=PANEL_BG, foreground=TEXT_FG)
        style.configure("Panel.TLabelframe.Label", background=PANEL_BG, foreground=TEXT_FG)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        dashboard = tk.Frame(self.notebook, background="#000000")
        dashboard.columnconfigure(0, minsize=230, weight=0)
        dashboard.columnconfigure(1, weight=1)
        dashboard.rowconfigure(0, weight=0)
        dashboard.rowconfigure(1, weight=1)
        self.notebook.add(dashboard, text="Dashboard")

        cells_page = tk.Frame(self.notebook, background="#000000")
        cells_page.columnconfigure(0, weight=1)
        cells_page.rowconfigure(0, weight=1)
        self.notebook.add(cells_page, text="Cell Values")

        temps_page = tk.Frame(self.notebook, background="#000000")
        temps_page.columnconfigure(0, weight=1)
        temps_page.rowconfigure(0, weight=1)
        self.notebook.add(temps_page, text="Temperature Sensors")

        top = tk.Frame(dashboard, background=PANEL_BG, highlightthickness=3, highlightbackground="#8a8a8a")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=14, pady=(14, 8))
        top.columnconfigure(0, minsize=150, weight=0)
        top.columnconfigure(1, weight=1)
        top.rowconfigure(0, weight=1)

        self.logo_image = self._load_logo_image()
        logo_panel = tk.Frame(top, background=PANEL_BG, width=150, height=52)
        logo_panel.grid(row=0, column=0, sticky="nsew", padx=(10, 8), pady=8)
        logo_panel.grid_propagate(False)
        if self.logo_image is not None:
            tk.Label(logo_panel, image=self.logo_image, background=PANEL_BG).place(relx=0.5, rely=0.5, anchor="center")

        controls_panel = tk.Frame(top, background=PANEL_BG)
        controls_panel.grid(row=0, column=1, sticky="nsew", padx=(0, 12), pady=8)
        for column in range(14):
            controls_panel.columnconfigure(column, weight=1 if column in (2, 4, 7, 9, 11) else 0)

        ttk.Label(controls_panel, text="Connection", style="Panel.TLabel", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=(0, 14), pady=(8, 8)
        )
        self._add_top_combo(controls_panel, 1, "Model", self.product_model_var, PRODUCT_MODELS)
        self._add_top_combo(controls_panel, 3, "COM", self.port_var, [])
        refresh = ttk.Button(controls_panel, text="Refresh", command=self.refresh_ports)
        refresh.grid(row=0, column=5, sticky="ew", padx=4, pady=(8, 8))
        self.control_widgets.append(refresh)
        self._add_top_combo(controls_panel, 6, "Baud", self.baud_var, DEFAULT_BAUD_RATES)
        self.max_packs_spin = self._add_top_spin(
            controls_panel,
            8,
            "Max Packs",
            self.max_packs_var,
            1,
            PRODUCT_MODEL_MAX_PACKS[self.product_model_var.get()],
        )
        self._add_top_spin(controls_panel, 10, "Timeout", self.timeout_var, 0.2, 30, increment=0.1)

        buttons = ttk.Frame(controls_panel, style="Panel.TFrame")
        buttons.grid(row=0, column=12, columnspan=2, sticky="e", padx=(10, 0), pady=(8, 8))
        scan_btn = ttk.Button(buttons, text="Scan", command=self.scan)
        scan_btn.grid(row=0, column=0, padx=3)
        self.control_widgets.append(scan_btn)
        self.start_button = ttk.Button(buttons, text="Start", command=self.start)
        self.start_button.grid(row=0, column=1, padx=3)
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=2, padx=3)
        csv_btn = ttk.Button(buttons, text="Export CSV...", command=self.browse_csv)
        csv_btn.grid(row=0, column=3, padx=3)
        self.control_widgets.append(csv_btn)
        load_btn = ttk.Button(buttons, text="Load Log...", command=self.load_log)
        load_btn.grid(row=0, column=4, padx=3)
        self.control_widgets.append(load_btn)
        reset_btn = ttk.Button(buttons, text="Reset", command=self.reset_view)
        reset_btn.grid(row=0, column=5, padx=3)
        self.control_widgets.append(reset_btn)

        ttk.Label(controls_panel, text="Acquisition Rate", style="PanelMuted.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(2, 8)
        )
        self.interval_scale = ttk.Scale(
            controls_panel,
            from_=model_minimum_interval_seconds(self.product_model_var.get(), int(self.max_packs_var.get())),
            to=300,
            orient="horizontal",
            variable=self.interval_var,
            command=self._on_interval_changed,
        )
        self.interval_scale.grid(row=1, column=1, columnspan=11, sticky="ew", padx=(0, 8), pady=(2, 8))
        self.control_widgets.append(self.interval_scale)
        ttk.Label(controls_panel, textvariable=self.interval_label_var, style="Panel.TLabel", width=18).grid(
            row=1, column=12, columnspan=2, sticky="e", padx=(10, 0), pady=(2, 8)
        )
        self.max_packs_var.trace_add("write", lambda *_args: self._sync_interval_slider())

        left = tk.Frame(dashboard, background=PANEL_BG, highlightthickness=3, highlightbackground="#8a8a8a")
        left.grid(row=1, column=0, sticky="nsew", padx=(14, 8), pady=(8, 14))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        left_canvas = tk.Canvas(left, background=PANEL_BG, highlightthickness=0, width=205)
        left_scrollbar = tk.Scrollbar(
            left,
            orient="vertical",
            command=left_canvas.yview,
            width=10,
            background="#3a3a3a",
            troughcolor=PANEL_BG,
            activebackground="#6b7280",
            relief="flat",
            bd=0,
        )
        left_canvas.configure(yscrollcommand=left_scrollbar.set)
        left_canvas.grid(row=0, column=0, sticky="nsew")
        left_scrollbar.grid(row=0, column=1, sticky="ns")

        left_content = tk.Frame(left_canvas, background=PANEL_BG)
        left_window_id = left_canvas.create_window((0, 0), window=left_content, anchor="nw")
        left_content.bind("<Configure>", lambda _event: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
        left_canvas.bind("<Configure>", lambda event: left_canvas.itemconfigure(left_window_id, width=event.width))
        left_canvas.bind("<MouseWheel>", lambda event: left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"))

        ttk.Label(left_content, text="Live Status", style="Panel.TLabel", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 8)
        )

        row = 1
        for label, key in [
            ("Connection", "connection"),
            ("Link Status", "link_status"),
            ("Packet Status", "packet_status"),
            ("Packets", "packet_counts"),
            ("Last RX", "last_rx"),
            ("Last Error", "last_error"),
            ("Serial", "serial"),
            ("FW", "fw"),
            ("Total Voltage", "voltage"),
            ("Current", "current"),
            ("State", "state"),
            ("SOC", "soc"),
            ("Packs", "packs"),
            ("Cells", "cells"),
            ("Cell Min/Max", "cell_range"),
            ("Highest Cell", "highest_cell"),
            ("Lowest Cell", "lowest_cell"),
            ("Highest Temp", "highest_temp"),
            ("Lowest Temp", "lowest_temp"),
            ("Updated", "timestamp"),
            ("CSV File", "csv_file"),
        ]:
            ttk.Label(left_content, text=label, style="PanelMuted.TLabel", font=("Segoe UI", 8)).grid(row=row, column=0, sticky="w", padx=12, pady=(2, 0))
            row += 1
            if key in {"link_status", "packet_status", "last_error"}:
                value_label = tk.Label(
                    left_content,
                    textvariable=self.value_vars[key],
                    background=PANEL_BG,
                    foreground=TEXT_FG,
                    font=("Segoe UI", 9, "bold"),
                    wraplength=195,
                    justify="left",
                    anchor="w",
                )
                value_label.grid(row=row, column=0, sticky="w", padx=12, pady=(0, 5))
                self.status_value_labels[key] = value_label
            else:
                ttk.Label(left_content, textvariable=self.value_vars[key], style="Panel.TLabel", font=("Segoe UI", 9, "bold"), wraplength=195).grid(row=row, column=0, sticky="w", padx=12, pady=(0, 5))
            row += 1

        ttk.Label(left_content, text="Errors", style="PanelMuted.TLabel", font=("Segoe UI", 8)).grid(row=row, column=0, sticky="w", padx=12, pady=(8, 0))
        row += 1
        self.error_text = tk.Text(
            left_content,
            height=5,
            width=22,
            wrap="word",
            foreground=RED,
            background=PANEL_BG,
            insertbackground=TEXT_FG,
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            state="disabled",
        )
        self.error_text.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 8))
        row += 1
        ttk.Label(left_content, textvariable=self.status_var, style="PanelMuted.TLabel", wraplength=195).grid(row=row, column=0, sticky="ew", padx=12, pady=(8, 12))

        charts = ttk.Frame(dashboard, style="Dark.TFrame")
        charts.grid(row=1, column=1, sticky="nsew", padx=(6, 14), pady=(8, 14))
        charts.columnconfigure(0, weight=1)
        charts.rowconfigure(0, weight=1)
        charts.rowconfigure(1, weight=1)
        charts.rowconfigure(2, weight=1)
        charts.rowconfigure(3, weight=1)

        self.total_chart = TimeSeriesChart(charts, title="Total Voltage / Current / SOC", border_color=ORANGE, height=150, dual_axis=True)
        self.total_chart.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.pack_chart = TimeSeriesChart(charts, title="Pack Voltage", border_color=GREEN, height=165)
        self.pack_chart.grid(row=1, column=0, sticky="nsew", pady=8)
        self.cell_chart = TimeSeriesChart(charts, title="Cell Voltage", border_color=BLUE, height=210)
        self.cell_chart.grid(row=2, column=0, sticky="nsew", pady=8)
        self.temp_chart = TimeSeriesChart(charts, title="Temperature Sensors", border_color=PURPLE, height=150)
        self.temp_chart.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        tk.Label(
            dashboard,
            text="Programmed by Tatsuo",
            background="#000000",
            foreground="#6b7280",
            font=("Segoe UI", 8),
        ).place(relx=1.0, rely=1.0, anchor="se", x=-18, y=-4)

        self._build_cell_values_page(cells_page)
        self.temps_page = temps_page
        self._build_temperature_values_page(temps_page)

    def _load_logo_image(self) -> tk.PhotoImage | None:
        assets_dir = Path(__file__).with_name("assets")
        logo_path = assets_dir / "mes_logo_light.png"
        if not logo_path.exists():
            logo_path = assets_dir / "mes_logo.png"
        if not logo_path.exists():
            return None
        try:
            image = tk.PhotoImage(file=str(logo_path))
        except tk.TclError:
            return None
        width = max(image.width(), 1)
        target_width = 115
        subsample = max(1, round(width / target_width))
        return image.subsample(subsample, subsample)

    def _build_cell_values_page(self, parent: tk.Widget) -> None:
        parent.configure(background="#000000")
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        canvas = tk.Canvas(parent, background="#000000", highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        content = tk.Frame(canvas, background="#000000")
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))

        for column in range(2):
            content.columnconfigure(column, weight=1, uniform="packs")

        self.cell_value_labels.clear()
        self.pack_value_frames.clear()
        for pack_index in range(MAX_CELL_PACKS):
            pack_frame = tk.Frame(
                content,
                background=PANEL_BG,
                highlightthickness=2,
                highlightbackground="#333333",
            )
            pack_frame.grid(
                row=pack_index // 2,
                column=pack_index % 2,
                sticky="nsew",
                padx=8,
                pady=8,
            )
            self.pack_value_frames.append(pack_frame)

            tk.Label(
                pack_frame,
                text=f"Pack {pack_index + 1:02d}",
                background=PANEL_BG,
                foreground=TEXT_FG,
                font=("Segoe UI", 11, "bold"),
                anchor="w",
            ).grid(row=0, column=0, columnspan=4, sticky="ew", padx=10, pady=(8, 4))

            for column in range(4):
                pack_frame.columnconfigure(column, weight=1, uniform=f"pack{pack_index}")

            for cell_in_pack in range(16):
                cell_index = pack_index * 16 + cell_in_pack
                label = tk.Label(
                    pack_frame,
                    text=f"C{cell_index + 1:03d}\n--",
                    background="#111111",
                    foreground=MUTED_FG,
                    font=("Consolas", 10, "bold"),
                    justify="center",
                    width=12,
                    height=2,
                    relief="flat",
                    bd=0,
                )
                label.grid(
                    row=1 + cell_in_pack // 4,
                    column=cell_in_pack % 4,
                    sticky="nsew",
                    padx=5,
                    pady=5,
                )
                self.cell_value_labels.append(label)

        canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"))

    def _build_temperature_values_page(self, parent: tk.Widget) -> None:
        for child in parent.winfo_children():
            child.destroy()
        parent.configure(background="#000000")
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        canvas = tk.Canvas(parent, background="#000000", highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        content = tk.Frame(canvas, background="#000000")
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))

        for column in range(2):
            content.columnconfigure(column, weight=1, uniform="temp_groups")

        self.temp_value_labels.clear()
        self.temp_value_frames.clear()
        self.temp_group_title_labels.clear()

        model = self.product_model_var.get()
        if model == PRODUCT_PS5120E:
            group_specs = [(f"Pack {pack_index:02d}", 6) for pack_index in range(1, PRODUCT_MODEL_MAX_PACKS[PRODUCT_PS5120E] + 1)]
        else:
            group_specs = [("PDU", 4)] + [
                (f"Pack {pack_index:02d}", 6)
                for pack_index in range(1, PRODUCT_MODEL_MAX_PACKS[PRODUCT_HV140] + 1)
            ]
        sensor_number = 1
        for group_index, (title, sensor_count) in enumerate(group_specs):
            group_frame = tk.Frame(
                content,
                background=PANEL_BG,
                highlightthickness=2,
                highlightbackground=PURPLE if group_index == 0 else "#333333",
            )
            group_frame.grid(
                row=group_index // 2,
                column=group_index % 2,
                sticky="nsew",
                padx=8,
                pady=8,
            )
            self.temp_value_frames.append(group_frame)

            title_label = tk.Label(
                group_frame,
                text=title,
                background=PANEL_BG,
                foreground=TEXT_FG,
                font=("Segoe UI", 11, "bold"),
                anchor="w",
            )
            title_label.grid(row=0, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 4))
            self.temp_group_title_labels.append(title_label)

            for column in range(3):
                group_frame.columnconfigure(column, weight=1, uniform=f"temp_group{group_index}")

            for group_sensor_index in range(sensor_count):
                label = tk.Label(
                    group_frame,
                    text=f"{temperature_sensor_name(sensor_number, model=model)}\n--",
                    background="#111111",
                    foreground=MUTED_FG,
                    font=("Consolas", 11, "bold"),
                    justify="center",
                    width=14,
                    height=2,
                    relief="flat",
                    bd=0,
                )
                label.grid(
                    row=1 + group_sensor_index // 3,
                    column=group_sensor_index % 3,
                    sticky="nsew",
                    padx=6,
                    pady=6,
                )
                self.temp_value_labels.append(label)
                sensor_number += 1

        canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"))

    def _add_top_combo(
        self,
        parent: tk.Widget,
        column: int,
        label: str,
        variable: tk.Variable,
        values: list[int] | list[str],
    ) -> None:
        ttk.Label(parent, text=label, style="PanelMuted.TLabel").grid(
            row=0, column=column, sticky="e", padx=(4, 4), pady=12
        )
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=12)
        combo.grid(row=0, column=column + 1, sticky="ew", padx=(0, 8), pady=12)
        self.control_widgets.append(combo)
        if label == "COM":
            self.port_combo = combo

    def _add_top_spin(
        self,
        parent: tk.Widget,
        column: int,
        label: str,
        variable: tk.Variable,
        from_: float,
        to: float,
        *,
        increment: float = 1.0,
    ) -> ttk.Spinbox:
        ttk.Label(parent, text=label, style="PanelMuted.TLabel").grid(
            row=0, column=column, sticky="e", padx=(4, 4), pady=12
        )
        spin = ttk.Spinbox(parent, from_=from_, to=to, increment=increment, textvariable=variable, width=7)
        spin.grid(row=0, column=column + 1, sticky="ew", padx=(0, 8), pady=12)
        self.control_widgets.append(spin)
        return spin

    def _sync_interval_slider(self) -> None:
        try:
            max_packs = int(self.max_packs_var.get())
        except (tk.TclError, ValueError):
            return
        minimum = model_minimum_interval_seconds(self.product_model_var.get(), max_packs)
        if hasattr(self, "interval_scale"):
            self.interval_scale.configure(from_=minimum, to=300)
        if self.interval_var.get() < minimum:
            self.interval_var.set(float(minimum))
        self._on_interval_changed()

    def _on_interval_changed(self, _value: object | None = None) -> None:
        try:
            seconds = int(float(self.interval_var.get()) + 0.999)
        except (tk.TclError, ValueError):
            return
        if seconds != int(self.interval_var.get()):
            self.interval_var.set(float(seconds))
        minimum = model_minimum_interval_seconds(
            self.product_model_var.get(),
            safe_int(self.max_packs_var.get(), PRODUCT_MODEL_MAX_PACKS[self.product_model_var.get()]),
        )
        self.interval_label_var.set(f"{format_duration(seconds)}  min {minimum}s")

    def _add_combo(self, parent: tk.Widget, row: int, label: str, variable: tk.Variable, values: list[int] | list[str]) -> int:
        ttk.Label(parent, text=label, style="PanelMuted.TLabel").grid(row=row, column=0, sticky="w", padx=12, pady=(6, 0))
        row += 1
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly")
        combo.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 4))
        self.control_widgets.append(combo)
        if label == "COM":
            self.port_combo = combo
        return row + 1

    def _add_spin(
        self,
        parent: tk.Widget,
        row: int,
        label: str,
        variable: tk.Variable,
        from_: float,
        to: float,
        *,
        increment: float = 1.0,
    ) -> int:
        ttk.Label(parent, text=label, style="PanelMuted.TLabel").grid(row=row, column=0, sticky="w", padx=12, pady=(6, 0))
        row += 1
        spin = ttk.Spinbox(parent, from_=from_, to=to, increment=increment, textvariable=variable)
        spin.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 4))
        self.control_widgets.append(spin)
        return row + 1

    def refresh_ports(self) -> None:
        ports = available_ports()
        self.port_combo["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])
        if not ports:
            self.port_var.set("")
        self.status_var.set(f"Ports: {', '.join(ports) if ports else 'none'}")

    def _on_product_model_changed(self) -> None:
        model = self.product_model_var.get()
        max_allowed = PRODUCT_MODEL_MAX_PACKS.get(model, PRODUCT_MODEL_MAX_PACKS[PRODUCT_HV140])
        try:
            self.max_packs_spin.configure(to=max_allowed)
        except tk.TclError:
            pass
        current = safe_int(self.max_packs_var.get(), max_allowed)
        if current > max_allowed or current == PRODUCT_MODEL_MAX_PACKS.get(PRODUCT_HV140):
            self.max_packs_var.set(max_allowed)
        self.history.clear()
        self._build_temperature_values_page(self.temps_page)
        self._sync_interval_slider()
        protocol = PRODUCT_PROTOCOL_LABELS.get(model, "unknown")
        self.status_var.set(f"Model: {model} ({protocol})")

    def browse_csv(self) -> None:
        current_log = self.current_log_path
        if not self.running and current_log and current_log.exists():
            filename = filedialog.asksaveasfilename(
                title="Export current CSV log",
                defaultextension=".csv",
                filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
                initialdir=str(current_log.parent),
                initialfile=current_log.name,
            )
            if not filename:
                return
            destination = Path(filename)
            try:
                if current_log.resolve() != destination.resolve():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(current_log, destination)
                self.csv_var.set(str(destination))
                self.current_log_path = destination
                self.value_vars["csv_file"].set(str(destination))
                self.status_var.set(f"Exported CSV: {destination}")
                messagebox.showinfo("Export CSV", f"CSV exported to:\n{destination}")
            except Exception as exc:
                messagebox.showerror("Export CSV", f"Could not export CSV:\n{exc}")
            return

        filename = filedialog.asksaveasfilename(
            title="Select CSV log location",
            defaultextension=".csv",
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
            initialfile=Path(self.csv_var.get()).name or "bms_log.csv",
        )
        if filename:
            self.csv_var.set(filename)
            self.value_vars["csv_file"].set(f"Next: {filename}")
            self.status_var.set(f"Next CSV location: {filename}")

    def load_log(self) -> None:
        if self.running:
            messagebox.showwarning("Load Log", "Stop live collection before loading a log file.")
            return
        filename = filedialog.askopenfilename(
            title="Open BMS CSV log",
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
            initialdir=str(Path(self.csv_var.get()).parent if self.csv_var.get() else Path.cwd()),
        )
        if not filename:
            return
        try:
            samples = load_samples_from_csv(Path(filename))
        except Exception as exc:
            messagebox.showerror("Load Log", f"Could not load CSV:\n{exc}")
            return
        if not samples:
            messagebox.showwarning("Load Log", "The selected CSV has no readable samples.")
            return

        product_model = getattr(samples[-1], "product_model", "") or PRODUCT_HV140
        if product_model in PRODUCT_MODELS:
            self.product_model_var.set(product_model)
        self.history = samples
        self.current_log_path = Path(filename)
        self.value_vars["csv_file"].set(filename)
        self.record_events = [(0, "START", GREEN)]
        if len(samples) > 1:
            self.record_events.append((len(samples) - 1, "STOP", MUTED_FG))
        self.start_event_pending = False
        self.packet_ok_count = len(samples)
        self.packet_error_count = 0
        self.consecutive_packet_errors = 0
        self.link_stale = False
        self.csv_var.set(filename)
        self.value_vars["connection"].set("Loaded log")
        self.value_vars["link_status"].set("Log loaded")
        self.value_vars["packet_status"].set("Loaded")
        self.value_vars["packet_counts"].set(f"Rows {len(samples)}")
        self._set_link_health("idle")
        self._show_sample(samples[-1], connection="Loaded log", append=False, mark_packet=False)
        self.status_var.set(f"Loaded {len(samples)} samples from {filename}")

    def reset_view(self) -> None:
        if self.running:
            messagebox.showwarning("Reset", "Stop live collection before resetting.")
            return
        self.history.clear()
        self.current_log_path = None
        self.value_vars["csv_file"].set("--")
        self.record_events.clear()
        self.start_event_pending = False
        self.packet_ok_count = 0
        self.packet_error_count = 0
        self.consecutive_packet_errors = 0
        self.last_packet_ok_monotonic = None
        self.link_monitor_started_monotonic = None
        self.link_stale = False
        for key, variable in self.value_vars.items():
            if key == "link_status":
                variable.set("Idle")
            elif key == "packet_counts":
                variable.set("RX 0 / ERR 0 / Streak 0")
            else:
                variable.set("--")
        self.value_vars["packet_status"].set("--")
        self.status_var.set("Reset")
        self._set_errors([])
        self._set_link_health("idle")
        self.total_chart.set_series([])
        self.pack_chart.set_series([])
        self.cell_chart.set_series([])
        self.temp_chart.set_series([])
        self._clear_cell_values()
        self._clear_temperature_values()

    def scan(self) -> None:
        if self.running:
            return
        ports = available_ports()
        if not ports:
            messagebox.showwarning("Scan", "No COM ports found.")
            return
        self.status_var.set("Scanning...")
        self._set_controls_enabled(False)
        try:
            timeout = float(self.timeout_var.get())
            max_packs = int(self.max_packs_var.get())
        except ValueError as exc:
            messagebox.showerror("Settings", str(exc))
            self._set_controls_enabled(True)
            return
        threading.Thread(target=self._scan_worker, args=(ports, timeout, max_packs), daemon=True).start()

    def _scan_worker(self, ports: list[str], timeout: float, max_packs: int) -> None:
        product_model = self.product_model_var.get()
        try:
            results = scan_bms(
                ports=ports,
                baud_rates=DEFAULT_BAUD_RATES,
                response_timeout=timeout,
                enforce_checksum=True,
                invert_current=False,
                product_model=product_model,
                max_packs=max_packs,
            )
            if not results:
                self.events.put(("scan_done", None))
                return

            result = results[0]
            detected_model = result.product_model
            detected_max_packs = PRODUCT_MODEL_MAX_PACKS.get(detected_model, max_packs)
            with serial.Serial(
                port=result.port,
                baudrate=result.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
                write_timeout=1.0,
            ) as ser:
                sample = poll_bms(
                    ser,
                    response_timeout=timeout,
                    enforce_checksum=True,
                    invert_current=False,
                    pack_count=None,
                    cells_per_pack=None,
                    max_packs=detected_max_packs,
                    product_model=detected_model,
                )
            self.events.put(("scan_done", (result, sample)))
        except Exception as exc:
            self.events.put(("error", f"Scan failed: {exc}"))

    def start(self) -> None:
        if self.running:
            return
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("Start", "Select a COM port first.")
            return
        try:
            self._read_settings()
        except ValueError as exc:
            messagebox.showerror("Settings", str(exc))
            return
        self.stop_event.clear()
        self.running = True
        self.history.clear()
        self.record_events.clear()
        self.start_event_pending = True
        self.packet_ok_count = 0
        self.packet_error_count = 0
        self.consecutive_packet_errors = 0
        self.last_packet_ok_monotonic = None
        self.link_monitor_started_monotonic = time.monotonic()
        self.link_stale = False
        self.value_vars["link_status"].set("Opening port...")
        self._set_link_health("busy")
        self.value_vars["packet_status"].set("--")
        self.value_vars["packet_counts"].set("RX 0 / ERR 0 / Streak 0")
        self.value_vars["last_rx"].set("--")
        self.value_vars["last_error"].set("--")
        self._set_controls_enabled(False)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set(f"Running on {port}")
        self.worker = threading.Thread(target=self._collector_worker, daemon=True)
        self.worker.start()

    def _active_protocol_supported(self) -> bool:
        return self.product_model_var.get() in PRODUCT_MODELS

    def stop(self) -> None:
        if self.running:
            self.stop_event.set()
            self.status_var.set("Stopping...")

    def _collector_worker(self) -> None:
        product_model, port, baud, max_packs, interval, timeout, csv_path = self._read_settings()
        logger: CsvLogger | None = None
        try:
            with serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
                write_timeout=1.0,
            ) as ser:
                self.events.put(("connection", f"Connected to {port} @ {baud}"))
                self.events.put(("log", f"Connected to {port} @ {baud}; CSV will be created after first sample"))
                next_poll = time.monotonic()
                while not self.stop_event.is_set():
                    wait_time = next_poll - time.monotonic()
                    if wait_time > 0 and self.stop_event.wait(wait_time):
                        break
                    started_at = time.monotonic()
                    try:
                        sample = poll_bms(
                            ser,
                            response_timeout=timeout,
                            enforce_checksum=True,
                            invert_current=False,
                            pack_count=None,
                            cells_per_pack=None,
                            max_packs=max_packs,
                            product_model=product_model,
                        )
                        if logger is None:
                            log_path = build_log_path(csv_path, sample)
                            logger = CsvLogger(
                                log_path,
                                max_cells=max_packs * 16,
                                max_temps=self._max_temps_for_model(product_model, max_packs),
                            )
                            self.events.put(("log", f"CSV: {log_path}"))
                            self.events.put(("csv_path", str(log_path)))
                        logger.write(sample)
                        self.events.put(("sample", sample))
                    except (TimeoutError, JbdProtocolError, serial.SerialException, ValueError) as exc:
                        self.events.put(("packet_error", str(exc)))
                    next_poll += interval
                    if next_poll <= started_at:
                        next_poll = started_at + interval
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            if logger:
                logger.close()
            self.events.put(("stopped", None))

    def _read_settings(self) -> tuple[str, str, int, int, float, float, Path]:
        product_model = self.product_model_var.get()
        port = self.port_var.get().strip()
        baud = int(self.baud_var.get())
        max_packs = int(self.max_packs_var.get())
        interval = float(int(float(self.interval_var.get()) + 0.999))
        timeout = float(self.timeout_var.get())
        csv_path = Path(self.csv_var.get())
        max_allowed = PRODUCT_MODEL_MAX_PACKS.get(product_model, PRODUCT_MODEL_MAX_PACKS[PRODUCT_HV140])
        if max_packs < 1 or max_packs > max_allowed:
            raise ValueError(f"Max packs must be between 1 and {max_allowed}.")
        minimum = model_minimum_interval_seconds(product_model, max_packs)
        if interval < minimum:
            raise ValueError(f"Interval must be at least {minimum} seconds for {max_packs} packs.")
        if interval > 300:
            raise ValueError("Interval must be 300 seconds or less.")
        if timeout <= 0:
            raise ValueError("Timeout must be greater than 0.")
        return product_model, port, baud, max_packs, interval, timeout, csv_path

    def _max_temps_for_model(self, product_model: str, max_packs: int) -> int:
        if product_model == PRODUCT_PS5120E:
            return max_packs * 6
        return 4 + max_packs * 6

    def _drain_events(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "sample":
                self._show_sample(payload)
            elif event == "packet_error":
                self._show_packet_error(str(payload))
            elif event == "connection":
                self.value_vars["link_status"].set(str(payload))
                self._set_link_health("ok")
            elif event == "log":
                self.status_var.set(str(payload))
            elif event == "csv_path":
                self.csv_var.set(str(payload))
                self.current_log_path = Path(str(payload))
                self.value_vars["csv_file"].set(str(payload))
            elif event == "error":
                self._add_record_event("INT", RED)
                self.status_var.set(f"Error: {payload}")
                self.value_vars["link_status"].set("Stopped by error")
                self._set_link_health("error")
                self.value_vars["last_error"].set(str(payload))
                messagebox.showerror("BMS Collector", str(payload))
            elif event == "stopped":
                self.running = False
                self._add_record_event("STOP", MUTED_FG)
                self._set_controls_enabled(True)
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.value_vars["link_status"].set("Idle")
                self._set_link_health("idle")
                self._show_busy_message("Rendering full log", "Rendering the full chart. Please wait...")
                self.after(80, self._finish_stopped_render)
            elif event == "scan_done":
                self._set_controls_enabled(True)
                if payload:
                    result, sample = payload
                    self.product_model_var.set(result.product_model)
                    self.port_var.set(result.port)
                    self.baud_var.set(result.baud)
                    self.status_var.set(f"Found {result.product_model} on {result.port} @ {result.baud}")
                    self._show_sample(sample, connection=f"{result.port} @ {result.baud}")
                else:
                    message = "No supported BMS found. Check wiring, COM port, baud rate, and model support (HV140 / PS5120E)."
                    self.status_var.set("No BMS found")
                    self.value_vars["link_status"].set("No BMS response")
                    self.value_vars["packet_status"].set("Scan failed")
                    self._set_link_health("error")
                    messagebox.showwarning("Scan failed", message)
        self.after(150, self._drain_events)

    def _show_sample(
        self,
        sample: object,
        *,
        connection: str | None = None,
        append: bool = True,
        mark_packet: bool = True,
    ) -> None:
        if mark_packet:
            self._show_packet_ok(sample)
        if append:
            self.history.append(sample)
        if append and self.start_event_pending:
            self._add_record_event("START", GREEN)
            self.start_event_pending = False

        if connection:
            self.value_vars["connection"].set(connection)
        elif self.port_var.get():
            self.value_vars["connection"].set(f"{self.port_var.get()} @ {self.baud_var.get()}")
        self.value_vars["serial"].set(sample.serial_number or "--")
        self.value_vars["fw"].set(str(sample.software_version or "--"))
        self.value_vars["voltage"].set(format_value(sample.voltage_v, " V"))
        self.value_vars["current"].set(format_value(sample.current_a, " A"))
        self.value_vars["state"].set(charge_discharge_state(sample))
        self.value_vars["soc"].set(format_value(sample.soc_percent, " %"))
        self.value_vars["packs"].set(str(sample.configured_pack_count or "--"))
        self.value_vars["cells"].set(f"{sample.total_cell_count or '--'} total / {len(sample.cell_voltages_mv) or 0} read")
        online_cells = [value for value in sample.cell_voltages_mv if value is not None]
        if online_cells:
            self.value_vars["cell_range"].set(f"{min(online_cells)} / {max(online_cells)} mV")
            high_index, high_value = indexed_max(sample.cell_voltages_mv)
            low_index, low_value = indexed_min(sample.cell_voltages_mv)
            self.value_vars["highest_cell"].set(
                f"C{high_index:03d}  {high_value} mV" if high_index is not None else "--"
            )
            self.value_vars["lowest_cell"].set(
                f"C{low_index:03d}  {low_value} mV" if low_index is not None else "--"
            )
        else:
            self.value_vars["cell_range"].set("--")
            self.value_vars["highest_cell"].set("--")
            self.value_vars["lowest_cell"].set("--")

        if sample.temperatures_c:
            high_temp_index, high_temp = indexed_max(sample.temperatures_c)
            low_temp_index, low_temp = indexed_min(sample.temperatures_c)
            self.value_vars["highest_temp"].set(
                f"{sample_temperature_sensor_name(sample, high_temp_index)}  {high_temp:.1f} C"
                if high_temp_index is not None
                else "--"
            )
            self.value_vars["lowest_temp"].set(
                f"{sample_temperature_sensor_name(sample, low_temp_index)}  {low_temp:.1f} C"
                if low_temp_index is not None
                else "--"
            )
        else:
            self.value_vars["highest_temp"].set("--")
            self.value_vars["lowest_temp"].set("--")
        self.value_vars["timestamp"].set(sample.timestamp)
        self._set_errors(sample_error_codes(sample))
        self.status_var.set(f"Last update {sample.timestamp}")
        self._update_charts()
        self._update_cell_values(sample)
        self._update_temperature_values(sample)

    def _show_packet_ok(self, sample: object) -> None:
        self.packet_ok_count += 1
        self.consecutive_packet_errors = 0
        self.last_packet_ok_monotonic = time.monotonic()
        self.link_stale = False
        self.value_vars["link_status"].set("Online")
        self.value_vars["packet_status"].set("OK")
        self._set_link_health("ok")
        self.value_vars["packet_counts"].set(
            f"RX {self.packet_ok_count} / ERR {self.packet_error_count} / Streak 0"
        )
        self.value_vars["last_rx"].set(format_sample_time(sample.timestamp))
        self.value_vars["last_error"].set("--")

    def _show_packet_error(self, message: str) -> None:
        self.packet_error_count += 1
        self.consecutive_packet_errors += 1
        self._add_record_event("LOSS", ORANGE)
        self.link_stale = True
        now = datetime.now().strftime("%H:%M:%S")
        self.value_vars["link_status"].set("No response")
        self.value_vars["packet_status"].set(f"ERROR @ {now}")
        self._set_link_health("error")
        self.value_vars["packet_counts"].set(
            f"RX {self.packet_ok_count} / ERR {self.packet_error_count} / "
            f"Streak {self.consecutive_packet_errors}"
        )
        self.value_vars["last_error"].set(message)
        self.status_var.set(f"Read error @ {now}: {message}")
        self._update_charts()

    def _add_record_event(self, label: str, color: str) -> None:
        if not self.history:
            return
        sample_index = len(self.history) - 1
        if self.record_events and self.record_events[-1] == (sample_index, label, color):
            return
        self.record_events.append((sample_index, label, color))

    def _monitor_link_health(self) -> None:
        if self.running:
            now_monotonic = time.monotonic()
            stale_after = self._link_stale_after_seconds()
            reference = self.last_packet_ok_monotonic or self.link_monitor_started_monotonic
            if reference is not None:
                elapsed = now_monotonic - reference
                if elapsed > stale_after:
                    now_text = datetime.now().strftime("%H:%M:%S")
                    self.link_stale = True
                    self.value_vars["link_status"].set("No recent packet")
                    self.value_vars["packet_status"].set(f"STALE @ {now_text}")
                    self.value_vars["packet_counts"].set(
                        f"RX {self.packet_ok_count} / ERR {self.packet_error_count} / "
                        f"Streak {self.consecutive_packet_errors}"
                    )
                    self.value_vars["last_error"].set(
                        f"No successful packet for {int(elapsed)}s"
                    )
                    self._set_link_health("error")
                    self.status_var.set(
                        f"No successful packet for {int(elapsed)}s; waiting for next response..."
                    )
        self.after(500, self._monitor_link_health)

    def _link_stale_after_seconds(self) -> float:
        try:
            interval = float(self.interval_var.get())
        except (tk.TclError, ValueError):
            interval = 5.0
        try:
            timeout = float(self.timeout_var.get())
        except (tk.TclError, ValueError):
            timeout = 3.0
        return max(8.0, interval + timeout + 2.0, timeout * 2.0 + 2.0)

    def _set_link_health(self, state: str) -> None:
        if state == "ok":
            link_color = GREEN
            packet_color = GREEN
            error_color = MUTED_FG
        elif state == "error":
            link_color = RED
            packet_color = RED
            error_color = RED
        elif state == "busy":
            link_color = ORANGE
            packet_color = MUTED_FG
            error_color = MUTED_FG
        else:
            link_color = MUTED_FG
            packet_color = MUTED_FG
            error_color = MUTED_FG
        for key, color in {
            "link_status": link_color,
            "packet_status": packet_color,
            "last_error": error_color,
        }.items():
            label = self.status_value_labels.get(key)
            if label is not None:
                label.configure(foreground=color)

    def _show_busy_message(self, title: str, message: str) -> None:
        self._hide_busy_message()
        window = tk.Toplevel(self)
        window.title(title)
        window.configure(background=PANEL_BG)
        window.resizable(False, False)
        window.transient(self)
        window.grab_set()
        frame = tk.Frame(window, background=PANEL_BG, padx=22, pady=18)
        frame.grid(row=0, column=0, sticky="nsew")
        tk.Label(
            frame,
            text=message,
            background=PANEL_BG,
            foreground=TEXT_FG,
            font=("Segoe UI", 10, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 12))
        progress = ttk.Progressbar(frame, mode="indeterminate", length=260)
        progress.grid(row=1, column=0, sticky="ew")
        progress.start(12)
        window.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - window.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - window.winfo_height()) // 2)
        window.geometry(f"+{x}+{y}")
        self.busy_window = window
        self.busy_progress = progress
        self.status_var.set(message)

    def _hide_busy_message(self) -> None:
        if self.busy_progress is not None:
            try:
                self.busy_progress.stop()
            except tk.TclError:
                pass
            self.busy_progress = None
        if self.busy_window is not None:
            try:
                self.busy_window.grab_release()
                self.busy_window.destroy()
            except tk.TclError:
                pass
            self.busy_window = None

    def _finish_stopped_render(self) -> None:
        try:
            self._update_charts()
            self.status_var.set("Idle")
        finally:
            self._hide_busy_message()

    def _update_charts(self) -> None:
        samples = self._chart_samples()
        x_labels = [format_sample_time(sample.timestamp) for sample in samples]
        total_v = [sample.voltage_v for sample in samples]
        total_i = [sample.current_a for sample in samples]
        total_soc = [sample.soc_percent for sample in samples]
        total_reference_lines = self._total_voltage_reference_lines(samples)
        self.total_chart.set_series([
            ("Voltage", total_v, ORANGE, "left"),
            ("Current", total_i, CYAN, "right"),
            ("SOC", total_soc, "#facc15", "right"),
        ],
            x_labels=x_labels,
            reference_lines=total_reference_lines,
            event_markers=self._total_event_markers(samples, total_reference_lines),
        )

        max_packs = max((sample.configured_pack_count or 0 for sample in samples), default=0)
        pack_series = []
        pack_colors = color_cycle(max_packs)
        for pack_index in range(max_packs):
            values = [pack_voltage(sample, pack_index) for sample in samples]
            pack_series.append((f"P{pack_index + 1}", values, pack_colors[pack_index], "left"))
        self.pack_chart.set_series(
            pack_series,
            x_labels=x_labels,
            reference_lines=self._pack_voltage_reference_lines(samples),
            event_markers=self._pack_event_markers(samples),
        )

        max_cells = max((len(sample.cell_voltages_mv) for sample in samples), default=0)
        cell_series = []
        cell_colors = color_cycle(max_cells)
        for cell_index in range(max_cells):
            values = [
                sample.cell_voltages_mv[cell_index]
                if cell_index < len(sample.cell_voltages_mv)
                else None
                for sample in samples
            ]
            cell_series.append((f"C{cell_index + 1}", values, cell_colors[cell_index], "left"))
        self.cell_chart.set_series(
            cell_series,
            x_labels=x_labels,
            reference_lines=self._cell_voltage_reference_lines(samples),
            event_markers=self._cell_event_markers(samples),
        )

        max_temps = max((len(sample.temperatures_c) for sample in samples), default=0)
        temp_series = []
        temp_colors = color_cycle(max_temps)
        latest_sample = samples[-1] if samples else None
        for temp_index in range(max_temps):
            values = [
                sample.temperatures_c[temp_index]
                if temp_index < len(sample.temperatures_c)
                else None
                for sample in samples
            ]
            temp_series.append((
                sample_temperature_sensor_name(latest_sample, temp_index + 1),
                values,
                temp_colors[temp_index],
                "left",
            ))
        self.temp_chart.set_series(
            temp_series,
            x_labels=x_labels,
            reference_lines=self._temperature_reference_lines(samples),
            event_markers=self._temperature_event_markers(samples),
        )

    def _chart_samples(self) -> list[object]:
        if self.running:
            return self.history[-CHART_LIVE_SAMPLE_LIMIT:]
        return self.history

    def _temperature_reference_lines(self, samples: list[object]) -> list[tuple[float, str, str]]:
        for sample in reversed(samples):
            lines = getattr(sample, "temperature_warning_lines", None)
            if lines:
                return compact_temperature_reference_lines(lines)
        return [
            (-20.0, BLUE, "UT -20 C"),
            (50.0, ORANGE, "OT 50 C"),
            (70.0, RED, "OT 70 C"),
        ]

    def _cell_voltage_reference_lines(self, samples: list[object]) -> list[tuple[float, str, str]]:
        for sample in reversed(samples):
            lines = getattr(sample, "cell_voltage_warning_lines", None)
            if lines:
                return compact_limit_reference_lines(lines, unit="mV")
        return []

    def _pack_voltage_reference_lines(self, samples: list[object]) -> list[tuple[float, str, str]]:
        for sample in reversed(samples):
            lines = getattr(sample, "total_voltage_warning_lines_v", None)
            pack_count = sample.configured_pack_count or 0
            if lines and pack_count:
                pack_lines = [
                    (round(value / pack_count, 3), color, label)
                    for value, color, label in lines
                ]
                return compact_limit_reference_lines(pack_lines, unit="V")
        return []

    def _total_voltage_reference_lines(self, samples: list[object]) -> list[tuple[float, str, str]]:
        for sample in reversed(samples):
            lines = getattr(sample, "total_voltage_warning_lines_v", None)
            if lines:
                return compact_limit_reference_lines(lines, unit="V")
        return []

    def _error_event_markers(self, samples: list[object]) -> list[tuple[int, float | None, str, str, str]]:
        markers: list[tuple[int, float | None, str, str, str]] = self._record_event_markers(samples)
        active_codes: set[str] = set()
        for index, sample in enumerate(samples):
            current_codes = set(sample_error_codes(sample))
            for code in sorted(current_codes - active_codes):
                markers.append((index, None, event_code_color(code), event_code_label(code), "left"))
            active_codes = current_codes
        return markers

    def _record_event_markers(self, samples: list[object]) -> list[tuple[int, float | None, str, str, str]]:
        if not samples or not self.record_events:
            return []
        visible_offset = len(self.history) - len(samples)
        visible_count = len(samples)
        markers: list[tuple[int, float | None, str, str, str]] = []
        for sample_index, label, color in self.record_events:
            relative_index = sample_index - visible_offset
            if 0 <= relative_index < visible_count:
                markers.append((relative_index, None, color, label, "left"))
        return markers

    def _total_event_markers(
        self,
        samples: list[object],
        reference_lines: list[tuple[float, str, str]],
    ) -> list[tuple[int, float | None, str, str, str]]:
        markers = self._error_event_markers(samples)
        raw_reference_lines = self._raw_total_voltage_reference_lines(samples) or reference_lines
        markers.extend(
            threshold_event_markers(
                [sample.voltage_v for sample in samples],
                raw_reference_lines,
                prefix="V",
            )
        )
        return markers

    def _pack_event_markers(self, samples: list[object]) -> list[tuple[int, float | None, str, str, str]]:
        markers = self._error_event_markers(samples)
        reference_lines = self._raw_pack_voltage_reference_lines(samples) or self._pack_voltage_reference_lines(samples)
        max_packs = max((sample.configured_pack_count or 0 for sample in samples), default=0)
        for pack_index in range(max_packs):
            values = [pack_voltage(sample, pack_index) for sample in samples]
            markers.extend(threshold_event_markers(values, reference_lines, prefix=f"P{pack_index + 1}"))
        return markers

    def _cell_event_markers(self, samples: list[object]) -> list[tuple[int, float | None, str, str, str]]:
        markers = self._error_event_markers(samples)
        reference_lines = self._raw_cell_voltage_reference_lines(samples) or self._cell_voltage_reference_lines(samples)
        max_cells = max((len(sample.cell_voltages_mv) for sample in samples), default=0)
        for cell_index in range(max_cells):
            values = [
                sample.cell_voltages_mv[cell_index]
                if cell_index < len(sample.cell_voltages_mv)
                else None
                for sample in samples
            ]
            markers.extend(threshold_event_markers(values, reference_lines, prefix=f"C{cell_index + 1}"))
            states = [
                sample.cell_balance_states[cell_index]
                if cell_index < len(getattr(sample, "cell_balance_states", []))
                else None
                for sample in samples
            ]
            markers.extend(balance_event_markers(values, states, prefix=f"C{cell_index + 1}"))
        return markers

    def _temperature_event_markers(self, samples: list[object]) -> list[tuple[int, float | None, str, str, str]]:
        markers = self._error_event_markers(samples)
        reference_lines = self._raw_temperature_reference_lines(samples) or self._temperature_reference_lines(samples)
        max_temps = max((len(sample.temperatures_c) for sample in samples), default=0)
        latest_sample = samples[-1] if samples else None
        for temp_index in range(max_temps):
            values = [
                sample.temperatures_c[temp_index]
                if temp_index < len(sample.temperatures_c)
                else None
                for sample in samples
            ]
            markers.extend(
                threshold_event_markers(
                    values,
                    reference_lines,
                    prefix=sample_temperature_sensor_name(latest_sample, temp_index + 1),
                )
            )
        return markers

    def _raw_temperature_reference_lines(self, samples: list[object]) -> list[tuple[float, str, str]]:
        for sample in reversed(samples):
            lines = getattr(sample, "temperature_warning_lines", None)
            if lines:
                return lines
        return []

    def _raw_cell_voltage_reference_lines(self, samples: list[object]) -> list[tuple[float, str, str]]:
        for sample in reversed(samples):
            lines = getattr(sample, "cell_voltage_warning_lines", None)
            if lines:
                return lines
        return []

    def _raw_total_voltage_reference_lines(self, samples: list[object]) -> list[tuple[float, str, str]]:
        for sample in reversed(samples):
            lines = getattr(sample, "total_voltage_warning_lines_v", None)
            if lines:
                return lines
        return []

    def _raw_pack_voltage_reference_lines(self, samples: list[object]) -> list[tuple[float, str, str]]:
        for sample in reversed(samples):
            lines = getattr(sample, "total_voltage_warning_lines_v", None)
            pack_count = sample.configured_pack_count or 0
            if lines and pack_count:
                return [
                    (round(value / pack_count, 3), color, label)
                    for value, color, label in lines
                ]
        return []

    def _update_cell_values(self, sample: object) -> None:
        cells = sample.cell_voltages_mv
        read_count = len(cells)
        configured_packs = sample.configured_pack_count or 0
        cells_per_pack = sample.cells_per_pack or 16
        expected_count = min(configured_packs * cells_per_pack, len(self.cell_value_labels))

        online_values = [value for value in cells if value is not None]
        high_index = low_index = None
        if online_values:
            high_index, _high_value = indexed_max(cells)
            low_index, _low_value = indexed_min(cells)

        for index, label in enumerate(self.cell_value_labels):
            cell_number = index + 1
            if index < read_count:
                value = cells[index]
                if value is None:
                    text = f"C{cell_number:03d}\nnot detected"
                    fg = RED
                    bg = "#1a0d0d"
                else:
                    text = f"C{cell_number:03d}\n{value} mV"
                    fg = TEXT_FG
                    bg = "#111111"
                    if high_index == cell_number:
                        fg = ORANGE
                    elif low_index == cell_number:
                        fg = CYAN
                label.configure(text=text, foreground=fg, background=bg)
            elif index < expected_count:
                label.configure(text=f"C{cell_number:03d}\n--", foreground=MUTED_FG, background="#111111")
            else:
                label.configure(text=f"C{cell_number:03d}\n--", foreground="#4b5563", background="#0b0b0b")

        for pack_index, frame in enumerate(self.pack_value_frames):
            pack_start = pack_index * 16
            if pack_start < expected_count:
                frame.configure(highlightbackground="#4b5563")
            else:
                frame.configure(highlightbackground="#222222")

    def _update_temperature_values(self, sample: object) -> None:
        temperatures = sample.temperatures_c
        configured_packs = sample.configured_pack_count or 0
        if getattr(sample, "product_model", self.product_model_var.get()) == PRODUCT_PS5120E:
            expected_count = min(configured_packs * 6, len(self.temp_value_labels))
        else:
            expected_count = min(4 + configured_packs * 6, len(self.temp_value_labels))
        high_index = low_index = None
        if temperatures:
            high_index, _high_temp = indexed_max(temperatures)
            low_index, _low_temp = indexed_min(temperatures)

        for index, label in enumerate(self.temp_value_labels):
            sensor_number = index + 1
            sensor_name = sample_temperature_sensor_name(sample, sensor_number)
            if index >= len(temperatures):
                fg = MUTED_FG if index < expected_count else "#4b5563"
                bg = "#111111" if index < expected_count else "#0b0b0b"
                label.configure(text=f"{sensor_name}\n--", foreground=fg, background=bg)
                continue

            temperature = temperatures[index]
            fg = TEXT_FG
            bg = "#111111"
            if temperature <= -20:
                fg = BLUE
                bg = "#07111f"
            elif temperature >= 70:
                fg = RED
                bg = "#1a0d0d"
            elif temperature >= 50:
                fg = ORANGE
                bg = "#1a1208"
            elif high_index == sensor_number:
                fg = ORANGE
            elif low_index == sensor_number:
                fg = CYAN

            label.configure(text=f"{sensor_name}\n{temperature:.1f} C", foreground=fg, background=bg)

        for group_index, frame in enumerate(self.temp_value_frames):
            if getattr(sample, "product_model", self.product_model_var.get()) == PRODUCT_PS5120E:
                group_start = group_index * 6
            else:
                group_start = 0 if group_index == 0 else 4 + (group_index - 1) * 6
            if group_start < expected_count:
                frame.configure(
                    highlightbackground=PURPLE
                    if group_index == 0 and getattr(sample, "product_model", "") != PRODUCT_PS5120E
                    else "#4b5563"
                )
            else:
                frame.configure(highlightbackground="#222222")

    def _clear_cell_values(self) -> None:
        for index, label in enumerate(self.cell_value_labels, start=1):
            label.configure(text=f"C{index:03d}\n--", foreground="#4b5563", background="#0b0b0b")
        for frame in self.pack_value_frames:
            frame.configure(highlightbackground="#222222")

    def _clear_temperature_values(self) -> None:
        model = self.product_model_var.get()
        for index, label in enumerate(self.temp_value_labels, start=1):
            label.configure(
                text=f"{temperature_sensor_name(index, model=model)}\n--",
                foreground="#4b5563",
                background="#0b0b0b",
            )
        for frame in self.temp_value_frames:
            frame.configure(highlightbackground="#222222")

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in self.control_widgets:
            try:
                if isinstance(widget, ttk.Combobox):
                    widget.configure(state="readonly" if enabled else "disabled")
                else:
                    widget.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass
        if self.running:
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
        else:
            self.start_button.configure(state="normal" if enabled else "disabled")
            self.stop_button.configure(state="disabled")

    def _set_errors(self, errors: list[str]) -> None:
        self.error_text.configure(state="normal")
        self.error_text.delete("1.0", "end")
        self.error_text.insert("end", "\n".join(errors) if errors else "")
        self.error_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.running:
            self.stop_event.set()
            self.after(300, self.destroy)
        else:
            self.destroy()


def padded_range(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 1.0
    low = min(values)
    high = max(values)
    if low == high:
        pad = abs(low) * 0.05 or 1.0
    else:
        pad = (high - low) * 0.08
    return low - pad, high + pad


def format_axis(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def load_samples_from_csv(path: Path) -> list[BmsSample]:
    samples: list[BmsSample] = []
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        for row in reader:
            timestamp = row.get("timestamp", "").strip()
            if not timestamp:
                continue
            sample = BmsSample(timestamp=timestamp)
            sample.product_model = row.get("product_model", "").strip() or infer_product_model_from_filename(path)
            sample.voltage_v = parse_optional_float(row.get("voltage_v"))
            sample.current_a = parse_optional_float(row.get("current_a"))
            sample.remaining_capacity_ah = parse_optional_float(row.get("remaining_capacity_ah"))
            sample.full_capacity_ah = parse_optional_float(row.get("full_capacity_ah"))
            sample.soc_percent = parse_optional_float(row.get("soc_percent"))
            sample.cycle_count = parse_optional_int(row.get("cycle_count"))
            sample.serial_number = row.get("serial_number", "").strip()
            sample.configured_pack_count = parse_optional_int(row.get("configured_pack_count"))
            sample.cells_per_pack = parse_optional_int(row.get("cells_per_pack"))
            sample.total_cell_count = parse_optional_int(row.get("total_cell_count"))
            sample.ntc_count = parse_optional_int(row.get("ntc_count"))
            sample.protection_status = parse_optional_int(row.get("protection_status"))
            sample.fet_status = parse_optional_int(row.get("fet_status"))
            sample.alarm_status = parse_optional_int(row.get("alarm_status"))
            sample.fault_status = parse_optional_int(row.get("fault_status"))
            sample.charge_state = parse_optional_int(row.get("charge_state"))
            sample.software_version = row.get("software_version", "").strip() or None
            sample.basic_checksum_ok = parse_optional_bool(row.get("basic_checksum_ok"))
            sample.cells_checksum_ok = parse_optional_bool(row.get("cells_checksum_ok"))
            sample.basic_raw = row.get("basic_raw", "")
            sample.config_raw = row.get("config_raw", "")
            sample.cells_raw = row.get("cells_raw", "")
            sample.stats_raw = row.get("stats_raw", "")
            sample.temperatures_c = parse_indexed_float_columns(row, "temp_", "_c")
            sample.cell_voltages_mv = [
                parse_optional_int(row[key])
                for key in sorted_indexed_keys(row, "cell_", "_mv")
                if row.get(key, "").strip() != ""
            ]
            sample.ntc_count = sample.ntc_count or len(sample.temperatures_c)
            sample.total_cell_count = sample.total_cell_count or len(sample.cell_voltages_mv)
            if sample.product_model == PRODUCT_PS5120E:
                sample.cell_voltage_warning_lines = [
                    (2500.0, RED, "PS5120E UV 2500 mV"),
                    (3650.0, RED, "PS5120E OV 3650 mV"),
                ]
                sample.total_voltage_warning_lines_v = [
                    (40.0, RED, "PS5120E UV 40.0 V"),
                    (58.4, RED, "PS5120E OV 58.4 V"),
                ]
                pack_count = sample.configured_pack_count or 0
                sample.temperature_sensor_names = [
                    f"P{pack_index:02d}-S{sensor_index:02d}"
                    for pack_index in range(1, pack_count + 1)
                    for sensor_index in range(1, 7)
                ][: len(sample.temperatures_c)]
            csv_errors = [
                error.strip()
                for error in row.get("error_codes", "").split(";")
                if error.strip()
            ]
            sample.pace_warn_errors = csv_errors
            samples.append(sample)
    return samples


def infer_product_model_from_filename(path: Path) -> str:
    name = path.stem.upper()
    for model in PRODUCT_MODELS:
        if model.upper() in name:
            return model
    return PRODUCT_HV140


def sorted_indexed_keys(row: dict[str, str], prefix: str, suffix: str) -> list[str]:
    def key_index(key: str) -> int:
        middle = key[len(prefix) : len(key) - len(suffix)]
        try:
            return int(middle)
        except ValueError:
            return 0

    return sorted(
        [key for key in row if key.startswith(prefix) and key.endswith(suffix)],
        key=key_index,
    )


def parse_indexed_float_columns(row: dict[str, str], prefix: str, suffix: str) -> list[float]:
    values: list[float] = []
    for key in sorted_indexed_keys(row, prefix, suffix):
        value = parse_optional_float(row.get(key))
        if value is not None:
            values.append(value)
    return values


def parse_optional_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_optional_int(value: object) -> int | None:
    number = parse_optional_float(value)
    if number is None:
        return None
    return int(number)


def parse_optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def color_cycle(count: int) -> list[str]:
    palette = [
        "#22c55e",
        "#38bdf8",
        "#f97316",
        "#a78bfa",
        "#facc15",
        "#fb7185",
        "#2dd4bf",
        "#c084fc",
        "#84cc16",
        "#60a5fa",
        "#f59e0b",
        "#e879f9",
    ]
    return [palette[index % len(palette)] for index in range(count)]


def pack_voltage(sample: object, pack_index: int) -> float | None:
    pack_voltages = getattr(sample, "pack_voltages_v", None)
    if pack_voltages and pack_index < len(pack_voltages):
        return pack_voltages[pack_index]
    cells_per_pack = sample.cells_per_pack or 16
    start = pack_index * cells_per_pack
    end = start + cells_per_pack
    if len(sample.cell_voltages_mv) < end:
        return None
    cells = sample.cell_voltages_mv[start:end]
    if not cells or any(value is None for value in cells):
        return None
    return round(sum(value for value in cells if value is not None) / 1000.0, 3)


def indexed_max(values: list[float | int | None]) -> tuple[int | None, float | int | None]:
    indexed = [(index, value) for index, value in enumerate(values, start=1) if value is not None]
    if not indexed:
        return None, None
    return max(indexed, key=lambda item: item[1])


def indexed_min(values: list[float | int | None]) -> tuple[int | None, float | int | None]:
    indexed = [(index, value) for index, value in enumerate(values, start=1) if value is not None]
    if not indexed:
        return None, None
    return min(indexed, key=lambda item: item[1])


def sample_temperature_sensor_name(sample: object | None, sensor_number: int | None) -> str:
    if sensor_number is None:
        return "--"
    names = getattr(sample, "temperature_sensor_names", None) if sample is not None else None
    if names and 0 <= sensor_number - 1 < len(names):
        return str(names[sensor_number - 1])
    model = getattr(sample, "product_model", None) if sample is not None else None
    return temperature_sensor_name(sensor_number, model=model)


def temperature_sensor_name(sensor_number: int | None, *, model: str | None = None) -> str:
    if sensor_number is None:
        return "--"
    if model == PRODUCT_PS5120E:
        zero_based = sensor_number - 1
        pack_number = zero_based // 6 + 1
        sensor_in_pack = zero_based % 6 + 1
        return f"P{pack_number:02d}-S{sensor_in_pack:02d}"
    if sensor_number <= 4:
        return f"PDU-{sensor_number:02d}"
    zero_based = sensor_number - 5
    pack_number = zero_based // 6 + 1
    sensor_in_pack = zero_based % 6 + 1
    return f"P{pack_number:02d}-S{sensor_in_pack:02d}"


def model_minimum_interval_seconds(product_model: str, pack_count: int) -> int:
    if product_model == PRODUCT_PS5120E:
        return 2
    return conservative_interval_seconds(pack_count)


def compact_temperature_reference_lines(
    lines: list[tuple[float, str, str]]
) -> list[tuple[float, str, str]]:
    ut_values = [value for value, _color, label in lines if "Low" in label]
    ot_values = [value for value, _color, label in lines if "High" in label]
    compact: list[tuple[float, str, str]] = []

    if ut_values:
        ut_min = min(ut_values)
        ut_max = max(ut_values)
        compact.append((ut_min, RED, f"UT min {format_axis(ut_min)} C"))
        if ut_max != ut_min:
            compact.append((ut_max, BLUE, f"UT max {format_axis(ut_max)} C"))

    if ot_values:
        ot_min = min(ot_values)
        ot_max = max(ot_values)
        compact.append((ot_min, ORANGE, f"OT min {format_axis(ot_min)} C"))
        if ot_max != ot_min:
            compact.append((ot_max, RED, f"OT max {format_axis(ot_max)} C"))

    return compact or lines


def compact_limit_reference_lines(
    lines: list[tuple[float, str, str]],
    *,
    unit: str,
) -> list[tuple[float, str, str]]:
    uv_values = [value for value, _color, label in lines if "UV" in label]
    ov_values = [value for value, _color, label in lines if "OV" in label]
    compact: list[tuple[float, str, str]] = []

    if uv_values:
        uv_min = min(uv_values)
        uv_max = max(uv_values)
        compact.append((uv_min, RED, f"UV min {format_axis(uv_min)} {unit}"))
        if uv_max != uv_min:
            compact.append((uv_max, BLUE, f"UV max {format_axis(uv_max)} {unit}"))

    if ov_values:
        ov_min = min(ov_values)
        ov_max = max(ov_values)
        compact.append((ov_min, ORANGE, f"OV min {format_axis(ov_min)} {unit}"))
        if ov_max != ov_min:
            compact.append((ov_max, RED, f"OV max {format_axis(ov_max)} {unit}"))

    return compact or lines


def threshold_event_markers(
    values: list[float | int | None],
    reference_lines: list[tuple[float, str, str]],
    *,
    prefix: str,
) -> list[tuple[int, float | None, str, str, str]]:
    markers: list[tuple[int, float | None, str, str, str]] = []
    thresholds = classify_threshold_lines(reference_lines)
    active_labels: set[str] = set()
    for index, value in enumerate(values):
        current_labels: set[str] = set()
        if value is not None:
            for threshold, color, label, direction, short_label in thresholds:
                if direction == "high" and value >= threshold:
                    current_labels.add(label)
                    if label not in active_labels:
                        markers.append((index, float(value), color, f"{prefix} {short_label}", "left"))
                elif direction == "low" and value <= threshold:
                    current_labels.add(label)
                    if label not in active_labels:
                        markers.append((index, float(value), color, f"{prefix} {short_label}", "left"))
        active_labels = current_labels
    return markers


def balance_event_markers(
    values: list[float | int | None],
    states: list[int | None],
    *,
    prefix: str,
) -> list[tuple[int, float | None, str, str, str]]:
    markers: list[tuple[int, float | None, str, str, str]] = []
    previous_state = 0
    for index, state in enumerate(states):
        value = values[index] if index < len(values) else None
        current_state = state or 0
        if current_state > 0 and current_state != previous_state:
            color = ORANGE if current_state == 1 else RED
            label = f"{prefix} B{current_state}"
            markers.append((index, float(value) if value is not None else None, color, label, "left"))
        previous_state = current_state
    return markers


def classify_threshold_lines(
    lines: list[tuple[float, str, str]]
) -> list[tuple[float, str, str, str, str]]:
    thresholds: list[tuple[float, str, str, str, str]] = []
    for value, color, label in lines:
        direction = threshold_direction(label)
        if not direction:
            continue
        short_label = threshold_short_label(label, direction)
        thresholds.append((value, color, label, direction, short_label))
    return thresholds


def threshold_direction(label: str) -> str | None:
    text = label.lower()
    if "ov" in text or "ot" in text or "high" in text or "above" in text:
        return "high"
    if "uv" in text or "ut" in text or "low" in text or "lower" in text:
        return "low"
    return None


def threshold_short_label(label: str, direction: str) -> str:
    text = label.upper()
    for token in ("L3", "L2", "L1", "OV", "UV", "OT", "UT"):
        if token in text:
            return token
    return "HI" if direction == "high" else "LO"


def event_code_label(code: str) -> str:
    text = code.strip()
    if text.startswith("A") and "-" in text:
        parts = text.split("-", 2)
        if len(parts) >= 2:
            return f"{parts[0]} {parts[1]}"
    if text.startswith("E") and "-" in text:
        return text.split("-", 1)[0]
    lowered = text.lower()
    pack = text.split(" ", 1)[0] if text.startswith("P") else ""
    prefix = f"{pack} " if pack else ""
    if "fault" in lowered:
        return f"{prefix}FAULT".strip()
    if "protect" in lowered:
        return f"{prefix}PROT".strip()
    if "warn" in lowered:
        if "cell" in lowered and "voltage" in lowered:
            return f"{prefix}CELL WARN".strip()
        if "total voltage" in lowered or "pack voltage" in lowered:
            return f"{prefix}PACK WARN".strip()
        if "temperature" in lowered:
            return f"{prefix}TEMP WARN".strip()
        return f"{prefix}WARN".strip()
    return "EVENT"


def event_code_color(code: str) -> str:
    lowered = code.lower()
    if code.startswith("E") or "fault" in lowered:
        return RED
    if "protect" in lowered or "l3" in lowered:
        return RED
    if "warn" in lowered or "l2" in lowered:
        return ORANGE
    if "l1" in lowered:
        return BLUE
    return RED


def conservative_interval_seconds(pack_count: int) -> int:
    pack_count = max(1, min(MAX_CELL_PACKS, pack_count))
    if pack_count <= 4:
        return 3
    if pack_count <= 7:
        return 4
    if pack_count <= 10:
        return 5
    if pack_count <= 14:
        return 6
    if pack_count <= 20:
        return 8
    if pack_count <= 25:
        return 10
    return 12


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if remainder:
        return f"{minutes}m {remainder}s"
    return f"{minutes}m"


def format_sample_time(timestamp: str) -> str:
    try:
        return datetime.fromisoformat(timestamp).strftime("%H:%M:%S")
    except ValueError:
        return timestamp[-8:]


def safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, tk.TclError):
        return default


def format_value(value: object, suffix: str) -> str:
    if value is None:
        return "--"
    return f"{value}{suffix}"


def main() -> None:
    app = BmsCollectorGui()
    app.mainloop()


if __name__ == "__main__":
    main()
