#!/usr/bin/env python
"""PyQt6 front-end for the cat printer: pick an image or ZPL label, preview, print."""
import asyncio
import logging
import os
import subprocess
import sys

import numpy as np
from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSlider, QSplitter, QVBoxLayout, QWidget,
)

if getattr(sys, 'frozen', False):
    # In the AppImage, PyInstaller points LD_LIBRARY_PATH at the bundled libs, which breaks the
    # host programs bleak runs on import (bluetoothctl). Give child processes the original env.
    _popen_init = subprocess.Popen.__init__

    def _popen_host_env(self, *args, **kwargs):
        if kwargs.get('env') is None:
            env = dict(os.environ)
            if 'LD_LIBRARY_PATH_ORIG' in env:
                env['LD_LIBRARY_PATH'] = env.pop('LD_LIBRARY_PATH_ORIG')
            else:
                env.pop('LD_LIBRARY_PATH', None)
            kwargs['env'] = env
        _popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _popen_host_env

from catprinter import logger  # noqa: E402
from catprinter.ble import run_ble
from catprinter.cmds import PRINT_WIDTH
from catprinter.img import read_img, read_img_gray_levels
from catprinter.zpl import is_zpl, zpl_to_img

ALGOS = ['floyd-steinberg', 'atkinson', 'halftone', 'mean-threshold', 'none']
ZPL_LAYOUTS = ['reflow', 'scale']

STYLE = """
* { font-size: 13px; }
QMainWindow, QWidget#root { background: #1e1f24; }
QLabel { color: #d7d9e0; }
QLabel#title { font-size: 20px; font-weight: 600; color: #ffffff; }
QLabel#hint { color: #8b8f9c; }
QFrame#card { background: #272930; border: 1px solid #33363f; border-radius: 10px; }
QLineEdit, QComboBox, QPlainTextEdit {
    background: #1e1f24; color: #e6e8ee; border: 1px solid #3a3d47;
    border-radius: 6px; padding: 6px 8px; selection-background-color: #ff7a9c;
}
QLineEdit:focus, QComboBox:focus { border-color: #ff7a9c; }
QComboBox QAbstractItemView { background: #272930; color: #e6e8ee; selection-background-color: #ff7a9c; }
QPushButton {
    background: #343743; color: #e6e8ee; border: none; border-radius: 6px; padding: 7px 14px;
}
QPushButton:hover { background: #3f4350; }
QPushButton:disabled { color: #6c707c; background: #2b2d36; }
QPushButton#primary { background: #ff7a9c; color: #1e1f24; font-weight: 600; padding: 10px; }
QPushButton#primary:hover { background: #ff93b0; }
QPushButton#primary:disabled { background: #5a4650; color: #8b7a82; }
QCheckBox { color: #d7d9e0; spacing: 8px; }
QSlider::groove:horizontal { height: 4px; background: #3a3d47; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #ff7a9c; border-radius: 2px; }
QSlider::handle:horizontal { background: #ffffff; width: 14px; margin: -6px 0; border-radius: 7px; }
QProgressBar { background: #1e1f24; border: none; border-radius: 3px; max-height: 6px; }
QProgressBar::chunk { background: #ff7a9c; border-radius: 3px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #3f4350; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QSplitter::handle { background: transparent; }
"""


def build_image(filename, algo, zpl_layout):
    """Same pipeline as print.py: returns the boolean image (True = black)."""
    if is_zpl(filename):
        with open(filename, encoding='utf-8', errors='replace') as f:
            return zpl_to_img(f.read(), PRINT_WIDTH, zpl_layout)
    return read_img(filename, PRINT_WIDTH, algo)


class LogBridge(logging.Handler, QObject):
    message = pyqtSignal(str)

    def __init__(self):
        logging.Handler.__init__(self)
        QObject.__init__(self)
        self.setFormatter(logging.Formatter('%(message)s'))

    def emit(self, record):
        self.message.emit(self.format(record))


class PreviewWorker(QThread):
    done = pyqtSignal(int, object)    # token, bool ndarray
    failed = pyqtSignal(int, str)

    def __init__(self, token, filename, algo, layout):
        super().__init__()
        self.token, self.args = token, (filename, algo, layout)

    def run(self):
        try:
            self.done.emit(self.token, build_image(*self.args))
        except Exception as e:  # noqa: BLE001 - show any failure in the UI
            self.failed.emit(self.token, str(e) or repr(e))


class PrintWorker(QThread):
    finished_ok = pyqtSignal(bool)
    cancelled = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, img, energy, device, gray_file):
        super().__init__()
        self.img, self.energy, self.device, self.gray_file = img, energy, device, gray_file
        self._loop = None
        self._task = None
        self._cancel_requested = False

    def cancel(self):
        """Cancels the BLE job from the GUI thread; the printer is disconnected on the way out."""
        self._cancel_requested = True
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)

    async def _job(self, gray):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        if self._cancel_requested:
            raise asyncio.CancelledError
        return await run_ble(self.img, energy=self.energy, device=self.device,
                             gray_levels=gray)

    def run(self):
        try:
            gray = read_img_gray_levels(self.gray_file, PRINT_WIDTH) if self.gray_file else None
            self.finished_ok.emit(bool(asyncio.run(self._job(gray))))
        except asyncio.CancelledError:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e) or repr(e))


def card():
    f = QFrame()
    f.setObjectName('card')
    return f


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Cat Printer')
        self.resize(1000, 700)
        self.setAcceptDrops(True)

        self.bin_img = None
        self.token = 0
        self.preview_worker = None
        self.print_worker = None

        self._build_ui()

        self.bridge = LogBridge()
        self.bridge.message.connect(self.append_log)
        logger.setLevel(logging.INFO)
        logger.addHandler(self.bridge)

    def closeEvent(self, e):
        if self.print_worker is not None:
            self.print_worker.cancel()
            self.print_worker.wait(5000)
        logger.removeHandler(self.bridge)
        super().closeEvent(e)

    # ---- UI ----------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        root.setObjectName('root')
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)

        # Left: options
        left = card()
        left.setFixedWidth(300)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(16, 16, 16, 16)
        lv.setSpacing(12)

        title = QLabel('🐱 Cat Printer')
        title.setObjectName('title')
        lv.addWidget(title)

        self.path = QLineEdit()
        self.path.setPlaceholderText('Arrastra un archivo o examina…')
        self.path.editingFinished.connect(self.update_preview)
        browse = QPushButton('Examinar')
        browse.clicked.connect(self.browse)
        prow = QHBoxLayout()
        prow.addWidget(self.path, 1)
        prow.addWidget(browse)
        lv.addLayout(prow)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setVerticalSpacing(10)
        self.algo = QComboBox()
        self.algo.addItems(ALGOS)
        self.algo.currentIndexChanged.connect(self.update_preview)
        self.layout_box = QComboBox()
        self.layout_box.addItems(ZPL_LAYOUTS)
        self.layout_box.currentIndexChanged.connect(self.update_preview)
        form.addRow('Binarización', self.algo)
        form.addRow('Diseño ZPL', self.layout_box)
        lv.addLayout(form)

        self.energy_label = QLabel('Energía: 10')
        self.energy = QSlider(Qt.Orientation.Horizontal)
        self.energy.setRange(1, 10)
        self.energy.setValue(10)
        self.energy.valueChanged.connect(
            lambda v: self.energy_label.setText(f'Energía: {v}'))
        lv.addWidget(self.energy_label)
        lv.addWidget(self.energy)

        self.gray = QCheckBox('16 niveles de gris (solo MXW01)')
        lv.addWidget(self.gray)

        lv.addWidget(QLabel('Dispositivo BLE'))
        self.device = QLineEdit()
        self.device.setPlaceholderText('Vacío = autodetectar')
        lv.addWidget(self.device)

        lv.addStretch(1)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        lv.addWidget(self.progress)
        self.print_btn = QPushButton('Imprimir')
        self.print_btn.setObjectName('primary')
        self.print_btn.setEnabled(False)
        self.print_btn.clicked.connect(self.print_job)
        lv.addWidget(self.print_btn)
        self.cancel_btn = QPushButton('Cancelar impresión')
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.cancel_job)
        lv.addWidget(self.cancel_btn)

        outer.addWidget(left)

        # Right: preview + log
        split = QSplitter(Qt.Orientation.Vertical)

        pv = card()
        pvl = QVBoxLayout(pv)
        pvl.setContentsMargins(16, 12, 16, 12)
        self.info = QLabel('Vista previa')
        self.info.setObjectName('hint')
        pvl.addWidget(self.info)
        self.preview = QLabel('Suelta aquí una imagen o archivo ZPL')
        self.preview.setObjectName('hint')
        self.preview.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.preview)
        pvl.addWidget(scroll, 1)
        split.addWidget(pv)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        split.addWidget(self.log)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        outer.addWidget(split, 1)

    # ---- helpers -----------------------------------------------------------
    def append_log(self, text):
        self.log.appendPlainText(text)

    def set_busy(self, busy):
        self.print_btn.setEnabled(not busy and self.bin_img is not None)
        self.print_btn.setVisible(not busy)
        self.cancel_btn.setVisible(busy)
        self.cancel_btn.setEnabled(busy)
        self.progress.setRange(0, 0 if busy else 1)

    def browse(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, 'Elegir archivo', '',
            'Imágenes y ZPL (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.zpl *.txt);;Todos (*)')
        if fn:
            self.load(fn)

    def load(self, fn):
        self.path.setText(fn)
        self.update_preview()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls and urls[0].isLocalFile():
            self.load(urls[0].toLocalFile())

    # ---- preview -----------------------------------------------------------
    def update_preview(self):
        fn = self.path.text().strip()
        if not fn:
            return
        self.token += 1
        self.bin_img = None
        self.print_btn.setEnabled(False)
        self.info.setText('⏳ Generando vista previa…')
        w = PreviewWorker(self.token, fn, self.algo.currentText(),
                          self.layout_box.currentText())
        w.done.connect(self.on_preview)
        w.failed.connect(self.on_preview_error)
        self.preview_worker = w  # keep a reference
        w.start()

    def on_preview(self, token, img):
        if token != self.token:
            return
        self.bin_img = img
        h, w = img.shape
        arr = np.ascontiguousarray(np.where(img, 0, 255).astype(np.uint8))
        qimg = QImage(arr.data, w, h, w, QImage.Format.Format_Grayscale8).copy()
        self.preview.setPixmap(QPixmap.fromImage(qimg))
        self.info.setText(f'Vista previa — {w} × {h} px')
        self.print_btn.setEnabled(self.print_worker is None)

    def on_preview_error(self, token, msg):
        if token != self.token:
            return
        self.info.setText('🛑 No se pudo generar la vista previa')
        self.append_log(f'🛑 {msg}')

    # ---- printing ----------------------------------------------------------
    def print_job(self):
        fn = self.path.text().strip()
        if self.bin_img is None or not fn:
            return
        gray_file = fn if self.gray.isChecked() and not is_zpl(fn) else None
        energy = self.energy.value() * 0xffff // 10
        w = PrintWorker(self.bin_img, energy, self.device.text().strip(), gray_file)
        w.finished_ok.connect(self.on_printed)
        w.cancelled.connect(self.on_cancelled)
        w.failed.connect(self.on_print_error)
        self.print_worker = w
        self.set_busy(True)
        w.start()

    def cancel_job(self):
        if self.print_worker is not None:
            self.cancel_btn.setEnabled(False)
            self.append_log('⏳ Cancelando…')
            self.print_worker.cancel()

    def on_cancelled(self):
        self.print_worker = None
        self.set_busy(False)
        self.append_log('🚫 Impresión cancelada.')

    def on_printed(self, ok):
        self.print_worker = None
        self.set_busy(False)
        self.append_log('🎉 Impresión terminada.' if ok
                        else '🛑 La impresión falló, revisa el registro.')

    def on_print_error(self, msg):
        self.print_worker = None
        self.set_busy(False)
        self.append_log(f'🛑 {msg}')


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    pal = app.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor('#1e1f24'))
    pal.setColor(QPalette.ColorRole.WindowText, QColor('#d7d9e0'))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
