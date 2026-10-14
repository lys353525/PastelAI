"""Pastel AI Studio — PySide6 채팅 클라이언트."""

from __future__ import annotations

import copy
import html
import shutil
import sys
import threading
from pathlib import Path

import markdown
from pygments import lex
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.token import Token

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QPixmap,
    QSyntaxHighlighter,
    QTextCharFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import codex_client
from core import (
    LANG_EXTENSIONS,
    MAX_ATTACHMENTS,
    SUPPORTED_FILES,
    LocalStore,
    atomic_text,
    code_blocks,
    new_id,
    split_code,
    validate_attachment,
)

APP_STYLE = """
QWidget {
    background:#FAF9FF; color:#465066;
    font-family:'Malgun Gothic'; font-size:13px;
}
QFrame#sidebar { background:#EFEAFF; border-radius:16px; }
QFrame#chatPanel {
    background: transparent; border:1px solid #E9E7F2; border-radius:16px;
}
QFrame#userBubble { background:#EAE4FF; border-radius:14px; }
QFrame#assistantBubble { background:#E9F8F0; border-radius:14px; }
QFrame#userBubble QLabel,
QFrame#assistantBubble QLabel {
    background: transparent;
    border: none;
}
QFrame#dropZone {
    background:#F8FFFC; border:2px dashed #B9E6D8; border-radius:10px;
}
QLabel#headline { font-size:19px; font-weight:700; background: transparent; }
QLabel#quiet { color:#80869B; background: transparent; }
QPushButton {
    background:#DDF4EC; border:0; border-radius:9px;
    padding:8px 11px; color:#345F54; font-weight:700;
}
QPushButton:hover { background:#C6EDDE; }
QPushButton:disabled { background:#ECEBF0; color:#ABB0BA; }
QPushButton#primary { background:#CEC3FA; color:#473B78; }
QPushButton#danger { background:#FCE1E7; color:#96506A; }
QComboBox, QTextEdit, QPlainTextEdit, QListWidget {
    background: transparent; border:1px solid #E1DFEC;
    border-radius:9px; padding:7px;
}
QListWidget::item { padding:10px; }
QListWidget::item:selected { background:#DBD2FF; color:#44396C; }
QScrollArea { border:0; background:transparent; }
"""


class TaskWorker(QThread):
    success = Signal(object)
    failure = Signal(str)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.success.emit(self.function())
        except Exception as exc:
            self.failure.emit(str(exc))


class ApiWorker(QThread):
    success = Signal(str)
    failure = Signal(str)
    cancelled = Signal()

    def __init__(self, model: str, store: LocalStore, chat: dict, parent=None):
        super().__init__(parent)
        self.model = model
        self.store = store
        self.chat = copy.deepcopy(chat)
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            prompt, images = codex_client.build_prompt(
                self.chat, self.store
            )
            text = codex_client.run_exec(
                prompt,
                images,
                self.model,
                self.cancel_event.is_set,
            )

            if self.cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.success.emit(text)

        except codex_client.GenerationCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failure.emit(str(exc))


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


class ColorHighlighter(QSyntaxHighlighter):
    """전체 문서를 분석하여 여러 줄 문자열과 주석 상태를 유지."""

    def __init__(self, document, language: str):
        super().__init__(document)

        try:
            self.lexer = get_lexer_by_name(
                language or "text",
                stripnl=False,
                ensurenl=False,
            )
        except Exception:
            self.lexer = TextLexer(stripnl=False, ensurenl=False)

        self.formats = []
        for category, color, bold in [
            (Token.Keyword, "#8164C8", True),
            (Token.String, "#19846A", False),
            (Token.Comment, "#9097A2", False),
            (Token.Number, "#B97448", False),
            (Token.Name.Function, "#426DBB", True),
            (Token.Name.Class, "#426DBB", True),
            (Token.Operator, "#AD638E", False),
        ]:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            fmt.setFontWeight(
                QFont.Weight.Bold if bold else QFont.Weight.Normal
            )
            self.formats.append((category, fmt))

        self.cache_text = None
        self.block_formats = {}

    def rebuild_cache(self, source: str):
        self.cache_text = source
        self.block_formats = {}
        block_number = 0
        column = 0

        for token_type, value in lex(source, self.lexer):
            fmt = next(
                (
                    style for category, style in self.formats
                    if token_type in category
                ),
                None,
            )

            pieces = value.split("\n")

            for index, piece in enumerate(pieces):
                length = utf16_length(piece)

                if fmt is not None and length:
                    self.block_formats.setdefault(
                        block_number, []
                    ).append((column, length, fmt))

                column += length

                if index < len(pieces) - 1:
                    block_number += 1
                    column = 0

    def highlightBlock(self, text: str):
        source = self.document().toPlainText()

        if source != self.cache_text:
            self.rebuild_cache(source)

        for start, length, fmt in self.block_formats.get(
            self.currentBlock().blockNumber(), []
        ):
            self.setFormat(start, length, fmt)


def rich_markdown(raw: str) -> str:
    """원시 HTML을 차단하고 링크·이미지를 일반 텍스트로 변환."""
    import xml.etree.ElementTree as ET
    from markdown.extensions import Extension
    from markdown.treeprocessors import Treeprocessor

    class RemoveLinks(Treeprocessor):
        def run(self, root):
            for parent in list(root.iter()):
                for child in list(parent):
                    if child.tag == "a":
                        child.tag = "span"
                        child.attrib.clear()

                    elif child.tag == "img":
                        replacement = ET.Element("span")
                        replacement.text = child.get("alt") or "[이미지]"
                        replacement.tail = child.tail
                        index = list(parent).index(child)
                        parent.remove(child)
                        parent.insert(index, replacement)

            return root

    class SafeLinks(Extension):
        def extendMarkdown(self, md):
            md.treeprocessors.register(
                RemoveLinks(md), "remove_external_links", 5
            )

    return markdown.markdown(
        html.escape(raw),
        extensions=["tables", "nl2br", "sane_lists", SafeLinks()],
    )


class CodePanel(QFrame):
    def __init__(self, language: str, code: str, parent=None):
        super().__init__(parent)
        self.language = language
        self.code = code

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel(language or "text"))
        header.addStretch()

        copy_button = QPushButton("복사")
        copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(self.code)
        )
        header.addWidget(copy_button)

        save_button = QPushButton("코드 저장")
        save_button.clicked.connect(self.export_code)
        header.addWidget(save_button)
        layout.addLayout(header)

        editor = QPlainTextEdit()
        editor.setPlainText(code)
        editor.setReadOnly(True)
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        editor.setFont(QFont("Consolas", 10))
        editor.setFixedHeight(
            min(330, max(78, (code.count("\n") + 2) * 20))
        )
        self.highlighter = ColorHighlighter(
            editor.document(), language
        )
        layout.addWidget(editor)

    def export_code(self):
        extension = LANG_EXTENSIONS.get(
            self.language.lower(), ".txt"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "코드 저장", "generated" + extension,
            "모든 파일 (*.*)",
        )

        if path:
            try:
                atomic_text(Path(path), self.code.rstrip() + "\n")
            except OSError as exc:
                QMessageBox.warning(self, "저장 오류", str(exc))


class MessageBubble(QFrame):
    def __init__(self, message: dict, store: LocalStore, parent=None):
        super().__init__(parent)
        own = message.get("role") == "user"
        self.setObjectName(
            "userBubble" if own else "assistantBubble"
        )
        self.setMaximumWidth(820)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addWidget(QLabel("나 🌸" if own else "Pastel AI ✨"))

        text = message.get("text", "")
        content = (
            [("prose", text, "")]
            if own
            else split_code(text)
        )

        if not text.strip() and not own:
            pending = QLabel("응답을 작성하고 있어요…")
            pending.setObjectName("quiet")
            layout.addWidget(pending)

        for kind, body, language in content:
            if kind == "code":
                layout.addWidget(CodePanel(language, body))
            elif body.strip():
                label = QLabel()
                label.setTextFormat(Qt.TextFormat.RichText)
                label.setText(
                    html.escape(body).replace("\n", "<br>")
                    if own else rich_markdown(body)
                )
                label.setWordWrap(True)
                label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                label.setOpenExternalLinks(False)
                layout.addWidget(label)

        attachments = message.get("attachments", [])
        if attachments:
            label = QLabel(
                "📎 " + ", ".join(
                    str(item.get("name", "파일"))
                    for item in attachments
                )
            )
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)

        for artifact in message.get("artifacts", []):
            if artifact.get("kind") != "image":
                continue
            try:
                pixmap = QPixmap(
                    str(store.absolute(artifact["path"]))
                )
                if not pixmap.isNull():
                    label = QLabel()
                    label.setPixmap(pixmap.scaled(
                        400, 300,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    ))
                    layout.addWidget(label)
            except (OSError, ValueError):
                continue


class DropZone(QFrame):
    dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        label = QLabel("📎 파일을 끌어놓거나 '파일 첨부'를 눌러주세요")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls() and any(
            url.isLocalFile() for url in mime.urls()
        ):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls() and any(
            url.isLocalFile() for url in mime.urls()
        ):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if paths:
            self.dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class PromptEdit(QTextEdit):
    send_requested = Signal()
    dropped = Signal(list)

    def keyPressEvent(self, event):
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers()
            & Qt.KeyboardModifier.ControlModifier
        ):
            self.send_requested.emit()
        else:
            super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls() and any(
            url.isLocalFile() for url in mime.urls()
        ):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls() and any(
            url.isLocalFile() for url in mime.urls()
        ):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if paths:
            self.dropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class SettingsDialog(QDialog):
    def __init__(self, parent, model: str):
        super().__init__(parent)
        self.task = None
        self.login_process = None
        self.setWindowTitle("설정 · Pastel AI Studio")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        title = QLabel("ChatGPT 로그인 및 모델 설정")
        title.setObjectName("headline")
        layout.addWidget(title)

        self.state = QLabel("로그인 상태 확인 중…")
        self.state.setTextFormat(Qt.TextFormat.PlainText)
        self.state.setWordWrap(True)
        layout.addWidget(self.state)

        row = QHBoxLayout()
        self.login_button = QPushButton("브라우저 로그인")
        self.login_button.clicked.connect(self.do_login)
        row.addWidget(self.login_button)

        self.refresh_button = QPushButton("상태 새로고침")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)

        self.logout_button = QPushButton("로그아웃")
        self.logout_button.clicked.connect(self.do_logout)
        row.addWidget(self.logout_button)
        layout.addLayout(row)

        note = QLabel(
            "로그인 정보는 Codex가 관리합니다. "
            "이 앱은 아이디·별명·요금제를 조회하지 않습니다."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addWidget(QLabel("모델 · 비워 두면 Codex 기본 모델"))
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItem("")
        self.model.setCurrentText(model)
        layout.addWidget(self.model)

        self.finish_button = QPushButton("설정 완료")
        self.finish_button.clicked.connect(self.accept)
        layout.addWidget(self.finish_button)

        # 로그인 자식 프로세스의 종료를 GUI를 막지 않고 확인합니다.
        self.login_timer = QTimer(self)
        self.login_timer.setInterval(500)
        self.login_timer.timeout.connect(self.poll_login)
        self.login_timer.start()

        QTimer.singleShot(0, self.refresh)

    def set_task_busy(self, busy: bool):
        for button in (
            self.login_button,
            self.refresh_button,
            self.logout_button,
            self.finish_button,
        ):
            button.setEnabled(not busy)

    def begin_task(self, function):
        if self.task is not None:
            return

        self.set_task_busy(True)
        self.task = TaskWorker(function, self)
        self.task.success.connect(self.on_result)
        self.task.failure.connect(self.on_error)
        self.task.finished.connect(self.on_finished)
        self.task.start()

    def refresh(self):
        self.state.setText("ログイン状態を確認中…".replace(
            "ログイン状態を確認中…", "로그인 상태 확인 중…"
        ))
        self.begin_task(codex_client.login_status)

    def do_logout(self):
        if self.login_process is not None:
            QMessageBox.information(
                self, "로그인 진행 중",
                "브라우저 로그인 완료 또는 시간 초과 후 로그아웃해 주세요.",
            )
            return

        def operation():
            codex_client.logout()
            return codex_client.login_status()

        self.state.setText("로그아웃 중…")
        self.begin_task(operation)

    def do_login(self):
        if self.login_process is not None:
            self.state.setText("브라우저에서 로그인을 완료해 주세요.")
            return

        self.begin_task(
            lambda: ("login_started", codex_client.start_login())
        )

    def on_result(self, result):
        if result[0] == "login_started":
            self.login_process = result[1]
            self.state.setText(
                "브라우저에서 로그인을 완료해 주세요. "
                "완료되면 상태를 다시 확인합니다."
            )
        else:
            ok, message = result
            self.state.setText(
                ("✅ 로그인됨\n" if ok else "🔑 로그인 필요\n")
                + (message or "상태 정보 없음")
            )

    def on_error(self, error: str):
        self.state.setText("작업 실패: " + error)

    def on_finished(self):
        task = self.task
        self.task = None
        if task:
            task.deleteLater()
        self.set_task_busy(False)

    def poll_login(self):
        if self.login_process is None:
            return

        if self.login_process.poll() is not None:
            self.login_process = None
            if self.task is None:
                self.refresh()

    def accept(self):
        if self.task is not None:
            return

        if self.login_process is not None:
            QMessageBox.information(
                self, "로그인 진행 중",
                "브라우저 로그인이 종료된 뒤 설정을 완료해 주세요.",
            )
            return

        super().accept()

    def reject(self):
        if self.task is None and self.login_process is None:
            super().reject()

    def closeEvent(self, event):
        if self.task is not None or self.login_process is not None:
            event.ignore()
        else:
            super().closeEvent(event)


class GalleryDialog(QDialog):
    def __init__(self, parent, store: LocalStore, chat: dict):
        super().__init__(parent)
        self.setWindowTitle("이 대화의 이미지 갤러리")
        self.resize(760, 540)
        layout = QVBoxLayout(self)

        self.images = QListWidget()
        self.images.setViewMode(QListWidget.ViewMode.IconMode)
        self.images.setIconSize(QPixmap(150, 135).size())
        self.images.setGridSize(QPixmap(185, 170).size())
        self.images.itemDoubleClicked.connect(
            lambda item: self.preview()
        )
        layout.addWidget(self.images)

        for artifact in store.image_artifacts(chat):
            try:
                path = store.absolute(artifact["path"])
                pixmap = QPixmap(str(path))
                if pixmap.isNull():
                    continue

                item = QListWidgetItem(
                    QIcon(pixmap.scaled(
                        150, 135,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )),
                    str(artifact.get("name", path.name)),
                )
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                self.images.addItem(item)
            except (OSError, ValueError):
                continue

        row = QHBoxLayout()
        row.addWidget(QLabel(f"저장된 이미지: {self.images.count()}장"))
        row.addStretch()

        preview = QPushButton("크게 보기")
        preview.clicked.connect(self.preview)
        row.addWidget(preview)

        save = QPushButton("선택 이미지 저장")
        save.clicked.connect(self.save_selected)
        row.addWidget(save)
        layout.addLayout(row)

    def selected_path(self):
        item = self.images.currentItem()
        return (
            Path(item.data(Qt.ItemDataRole.UserRole))
            if item else None
        )

    def preview(self):
        path = self.selected_path()
        if not path or not path.is_file():
            QMessageBox.information(
                self, "이미지 선택", "이미지를 선택해 주세요."
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(path.name)
        dialog.resize(840, 680)
        layout = QVBoxLayout(dialog)
        canvas = QLabel()
        canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        canvas.setPixmap(QPixmap(str(path)).scaled(
            790, 600,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        layout.addWidget(canvas)
        dialog.exec()

    def save_selected(self):
        path = self.selected_path()
        if not path or not path.is_file():
            return

        destination, _ = QFileDialog.getSaveFileName(
            self, "이미지 저장", path.name, "모든 파일 (*.*)"
        )
        if destination:
            try:
                if path.resolve() != Path(destination).resolve():
                    shutil.copy2(path, destination)
            except OSError as exc:
                QMessageBox.warning(self, "저장 오류", str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.store = LocalStore()
        self.chats = self.store.load_chats()
        settings = self.store.settings()
        self.model = str(settings.get("model", ""))

        self.chat_id = None
        self.attachments = []
        self.worker = None
        self.auth_task = None
        self.pending_message = None
        self.pending_chat_id = None
        self.logged_in = False
        self.auth_known = False

        self.setWindowTitle("Pastel AI Studio ✿")
        self.resize(1240, 820)
        self.setMinimumSize(880, 600)
        self.build_ui()

        if self.chats:
            self.show_chat(next(reversed(self.chats)))
        else:
            self.new_chat()

        self.update_key_indicator()
        QTimer.singleShot(0, self.check_login)

        if self.store.warnings:
            QTimer.singleShot(0, self.show_load_warnings)

    def show_load_warnings(self):
        QMessageBox.warning(
            self,
            "저장 데이터 확인",
            "\n".join(self.store.warnings)
            + "\n\n읽지 못한 데이터가 있다면 기존 파일을 백업해 주세요.",
        )

    def generation_active(self):
        # finished 신호를 처리할 때까지 새 요청을 막습니다.
        return self.worker is not None

    def build_ui(self):
        base = QWidget()
        self.setCentralWidget(base)
        root = QHBoxLayout(base)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        left = QVBoxLayout(sidebar)

        brand = QLabel("✿ Pastel AI Studio")
        brand.setObjectName("headline")
        left.addWidget(brand)

        self.new_button = QPushButton("＋ 새 대화")
        self.new_button.clicked.connect(self.new_chat)
        left.addWidget(self.new_button)

        self.chat_list = QListWidget()
        self.chat_list.itemClicked.connect(
            lambda item: self.show_chat(
                item.data(Qt.ItemDataRole.UserRole)
            )
        )
        left.addWidget(self.chat_list, 1)

        self.delete_button = QPushButton("대화 삭제")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self.delete_chat)
        left.addWidget(self.delete_button)

        gallery = QPushButton("🖼️ 이미지 갤러리")
        gallery.clicked.connect(self.open_gallery)
        left.addWidget(gallery)

        folder = QPushButton("📂 대화 결과물 폴더")
        folder.clicked.connect(self.open_chat_folder)
        left.addWidget(folder)

        self.setting_button = QPushButton("⚙️ 설정 / ChatGPT 로그인")
        self.setting_button.clicked.connect(self.open_settings)
        left.addWidget(self.setting_button)

        self.key_indicator = QLabel()
        self.key_indicator.setTextFormat(Qt.TextFormat.PlainText)
        self.key_indicator.setWordWrap(True)
        left.addWidget(self.key_indicator)

        note = QLabel(
            "대화와 결과물은 PC에 저장됩니다.\n"
            "사용 가능 여부와 한도는 로그인 계정 및 서비스 상태에 따릅니다."
        )
        note.setWordWrap(True)
        note.setObjectName("quiet")
        left.addWidget(note)
        splitter.addWidget(sidebar)

        panel = QFrame()
        panel.setObjectName("chatPanel")
        right = QVBoxLayout(panel)

        header = QHBoxLayout()
        self.heading = QLabel("새로운 대화")
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setObjectName("headline")
        header.addWidget(self.heading, 1)

        for title, callback in (
            ("답변 저장", self.save_answer),
            ("코드 저장", self.save_last_code),
            ("이미지 저장", self.save_last_image),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            header.addWidget(button)

        right.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        stream = QWidget()
        self.stream_layout = QVBoxLayout(stream)
        self.stream_layout.addStretch()
        self.scroll.setWidget(stream)

# 답변 표시 후 레이아웃 높이가 달라져도 맨 아래로 이동합니다.
        self.scroll.verticalScrollBar().rangeChanged.connect(
            self.scroll_to_bottom
        )

        right.addWidget(self.scroll, 1)

        self.drop_zone = DropZone()
        self.drop_zone.dropped.connect(self.add_path_list)
        right.addWidget(self.drop_zone)

        self.attach_summary = QLabel("첨부파일 없음")
        self.attach_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.attach_summary.setWordWrap(True)
        right.addWidget(self.attach_summary)

        toolbar = QHBoxLayout()
        self.file_button = QPushButton("📎 파일 첨부")
        self.file_button.clicked.connect(self.pick_attachments)
        toolbar.addWidget(self.file_button)

        self.clear_files_button = QPushButton("첨부 취소")
        self.clear_files_button.clicked.connect(self.clear_attachments)
        toolbar.addWidget(self.clear_files_button)
        toolbar.addStretch()
        right.addLayout(toolbar)

        self.prompt = PromptEdit()
        self.prompt.setAcceptRichText(False)
        self.prompt.setFixedHeight(91)
        self.prompt.setPlaceholderText(
            "메시지를 입력하세요. Ctrl+Enter로 전송"
        )
        self.prompt.send_requested.connect(self.submit)
        self.prompt.dropped.connect(self.add_path_list)
        right.addWidget(self.prompt)

        bottom = QHBoxLayout()
        self.status = QLabel("로그인 상태 확인 중…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)

        self.stop_button = QPushButton("■ 중단")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_generation)
        bottom.addWidget(self.stop_button)

        self.send_button = QPushButton("전송 ➤")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.submit)
        bottom.addWidget(self.send_button)
        right.addLayout(bottom)

        splitter.addWidget(panel)
        splitter.setSizes([292, 930])

    def check_login(self):
        if self.auth_task is not None:
            return

        self.auth_known = False
        self.send_button.setEnabled(False)
        self.setting_button.setEnabled(False)
        self.update_key_indicator()

        self.auth_task = TaskWorker(codex_client.login_status, self)
        self.auth_task.success.connect(self.on_login_result)
        self.auth_task.failure.connect(self.on_login_failure)
        self.auth_task.finished.connect(self.on_auth_finished)
        self.auth_task.start()

    def on_login_result(self, result):
        self.logged_in, message = result
        self.auth_known = True
        self.update_key_indicator()
        self.status.setText(
            "준비 완료"
            if self.logged_in else "설정에서 ChatGPT 로그인을 해 주세요."
        )

    def on_login_failure(self, error):
        self.logged_in = False
        self.auth_known = True
        self.status.setText("로그인 상태 확인 실패: " + error)
        self.update_key_indicator()

    def on_auth_finished(self):
        task = self.auth_task
        self.auth_task = None
        if task:
            task.deleteLater()
        self.set_busy(self.generation_active())

    def update_key_indicator(self):
        state = (
            "로그인 상태 확인 중"
            if not self.auth_known
            else "🔒 ChatGPT 로그인됨"
            if self.logged_in
            else "🔑 로그인 필요"
        )
        self.key_indicator.setText(
            state + "\n모델: " + (self.model or "Codex 기본")
        )

    def open_settings(self):
        if self.generation_active() or self.auth_task is not None:
            return

        dialog = SettingsDialog(self, self.model)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            candidate = dialog.model.currentText().strip()
            try:
                self.store.save_settings({"model": candidate})
                self.model = candidate
            except OSError as exc:
                QMessageBox.warning(self, "설정 저장 실패", str(exc))

        self.update_key_indicator()
        self.check_login()

    def persist(self, show_dialog=True) -> bool:
        try:
            self.store.save_chats(self.chats)
            return True
        except Exception as exc:
            self.status.setText(
                "대화 저장 실패 · 현재 내용은 메모리에 남아 있습니다."
            )
            if show_dialog:
                QMessageBox.warning(self, "대화 저장 실패", str(exc))
            return False

    def refresh_chats(self):
        self.chat_list.clear()
        for cid, chat in reversed(list(self.chats.items())):
            item = QListWidgetItem(chat.get("title", "대화"))
            item.setData(Qt.ItemDataRole.UserRole, cid)
            self.chat_list.addItem(item)
            if cid == self.chat_id:
                self.chat_list.setCurrentItem(item)

    def new_chat(self):
        if self.generation_active():
            return

        previous = self.chat_id
        cid = new_id()
        self.chats[cid] = {"title": "새로운 대화", "messages": []}

        if not self.persist():
            self.chats.pop(cid, None)
            self.chat_id = previous
            return

        self.chat_id = cid
        self.clear_attachments()
        self.refresh_chats()
        self.render_chat()

    def show_chat(self, cid):
        if self.generation_active() or cid not in self.chats:
            return
        self.chat_id = cid
        self.clear_attachments()
        self.refresh_chats()
        self.render_chat()

    def delete_chat(self):
        if not self.chat_id or self.generation_active():
            return

        answer = QMessageBox.question(
            self,
            "대화 삭제",
            "이 대화와 저장된 결과물 및 첨부파일을 영구 삭제할까요?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        cid = self.chat_id
        previous = self.chats.copy()
        self.chats.pop(cid)

        if not self.persist():
            self.chats = previous
            return

        try:
            self.store.delete_chat_files(cid)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(
                self, "일부 파일 삭제 실패", str(exc)
            )

        self.chat_id = None
        if self.chats:
            self.show_chat(next(reversed(self.chats)))
        else:
            self.new_chat()

        self.refresh_chats()
        self.render_chat()

    def current_chat(self):
        return self.chats.get(
            self.chat_id,
            {"title": "새로운 대화", "messages": []},
        )

    def scroll_to_bottom(self, *_):
        scrollbar = self.scroll.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


    def render_chat(self):
        while self.stream_layout.count() > 1:
            item = self.stream_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        chat = self.current_chat()
        self.heading.setText(chat.get("title", "새로운 대화"))

        for message in chat.get("messages", []):
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)

            bubble = MessageBubble(message, self.store)
            if message.get("role") == "user":
                layout.addStretch(1)
                layout.addWidget(bubble, 6)
            else:
                layout.addWidget(bubble, 6)
                layout.addStretch(1)

            self.stream_layout.insertWidget(
                self.stream_layout.count() - 1, row
            )

        QTimer.singleShot(0, self.scroll_to_bottom)


    def pick_attachments(self):
        patterns = " ".join(
            "*" + suffix for suffix in sorted(SUPPORTED_FILES)
        )
        paths, _ = QFileDialog.getOpenFileNames(
            self, "첨부파일 선택", "",
            f"지원 파일 ({patterns});;모든 파일 (*.*)",
        )
        self.add_path_list(paths)

    def add_path_list(self, paths):
        if self.generation_active():
            return

        for path in paths:
            try:
                resolved = Path(path).resolve()
                if str(resolved) in self.attachments:
                    continue

                if len(self.attachments) >= MAX_ATTACHMENTS:
                    raise ValueError(
                        f"최대 {MAX_ATTACHMENTS}개까지 첨부할 수 있습니다."
                    )

                validate_attachment(resolved)
                self.attachments.append(str(resolved))

            except Exception as exc:
                QMessageBox.warning(self, "첨부 불가", str(exc))
                break

        self.update_attachments()

    def update_attachments(self):
        self.attach_summary.setText(
            "📎 " + ", ".join(
                Path(path).name for path in self.attachments
            )
            if self.attachments else "첨부파일 없음"
        )

    def clear_attachments(self):
        self.attachments.clear()
        if hasattr(self, "attach_summary"):
            self.update_attachments()

    def submit(self):
        if self.generation_active() or self.auth_task is not None:
            return

        if not self.logged_in:
            self.open_settings()
            return

        text = self.prompt.toPlainText().strip()
        if not text:
            return

        if len(text) > codex_client.MAX_PROMPT_CHARS:
            QMessageBox.warning(
                self, "입력 크기 초과", "질문을 줄여 주세요."
            )
            return

        if self.chat_id not in self.chats:
            self.new_chat()
            if self.chat_id not in self.chats:
                return

        cid = self.chat_id
        chat = self.chats[cid]
        old_title = chat["title"]
        old_count = len(chat["messages"])

        user = {
            "id": new_id(),
            "role": "user",
            "text": text,
            "prompt": text,
            "attachments": [],
        }
        assistant = {
            "id": new_id(),
            "role": "assistant",
            "text": "",
            "artifacts": [],
            "status": "pending",
        }

        try:
            for index, path in enumerate(self.attachments):
                user["attachments"].append(
                    self.store.archive_attachment(
                        cid, user["id"], Path(path), index
                    )
                )

            if not chat["messages"]:
                chat["title"] = (
                    text[:27] + ("…" if len(text) > 27 else "")
                )

            chat["messages"].extend([user, assistant])
            self.store.save_chats(self.chats)

        except Exception as exc:
            del chat["messages"][old_count:]
            chat["title"] = old_title

            cleanup_error = ""
            try:
                self.store.remove_attachments(user["attachments"])
            except Exception as cleanup_exc:
                cleanup_error = (
                    "\n일부 복사 파일 정리 실패: " + str(cleanup_exc)
                )

            QMessageBox.warning(
                self, "전송 준비 실패", str(exc) + cleanup_error
            )
            return

        self.pending_message = assistant
        self.pending_chat_id = cid
        self.prompt.clear()
        self.clear_attachments()
        self.refresh_chats()
        self.render_chat()
        self.set_busy(True)
        self.status.setText(
            "AI가 답변을 작성 중입니다… 완료 후 한 번에 표시됩니다."
        )

        self.worker = ApiWorker(self.model, self.store, chat, self)
        self.worker.success.connect(self.on_success)
        self.worker.failure.connect(self.on_failure)
        self.worker.cancelled.connect(self.on_cancelled)
        self.worker.finished.connect(self.on_worker_finished)

        try:
            self.worker.start()
        except Exception as exc:
            self.on_failure(str(exc))
            self.on_worker_finished()

    def set_busy(self, busy):
        for widget in (
            self.new_button,
            self.chat_list,
            self.delete_button,
            self.file_button,
            self.clear_files_button,
            self.drop_zone,
            self.prompt,
        ):
            widget.setEnabled(not busy)

        self.setting_button.setEnabled(
            not busy and self.auth_task is None
        )
        self.send_button.setEnabled(
            not busy and self.auth_task is None and self.auth_known
        )
        self.stop_button.setEnabled(busy)

    def finish_response(self, text, state):
        message = self.pending_message
        cid = self.pending_chat_id

        if message is None:
            return

        message["text"] = text
        message["status"] = state
        errors = []

        try:
            if state == "complete":
                try:
                    artifacts = self.store.archive_answer(cid, message)
                    self.status.setText(
                        f"완료 · 결과물 {len(artifacts)}개 자동 저장됨"
                    )
                except Exception as exc:
                    errors.append("결과물 저장 실패: " + str(exc))
                    self.status.setText("응답 완료 · 결과물 저장 실패")
            elif state == "cancelled":
                self.status.setText("생성이 중단되었습니다.")
            else:
                self.status.setText("요청 실패")

            try:
                self.store.save_chats(self.chats)
            except Exception as exc:
                errors.append("대화 저장 실패: " + str(exc))
                self.status.setText(
                    "대화 저장 실패 · 답변은 메모리에 남아 있습니다."
                )
        finally:
            self.pending_message = None
            self.pending_chat_id = None
            self.render_chat()

        if errors:
            QMessageBox.warning(
                self, "저장 오류", "\n".join(errors)
            )

    def on_success(self, text):
        self.finish_response(text, "complete")

    def on_failure(self, error):
        self.finish_response("⚠ 요청 실패: " + error, "failed")

    def on_cancelled(self):
        self.finish_response("생성이 중단되었습니다.", "cancelled")

    def on_worker_finished(self):
        worker = self.worker
        self.worker = None

        if worker:
            worker.deleteLater()

        if self.pending_message is not None:
            self.finish_response(
                "⚠ 요청 실패: 요청이 응답 없이 종료되었습니다.",
                "failed",
            )

        self.set_busy(False)

    def stop_generation(self):
        if self.worker is not None:
            self.worker.cancel()
            self.stop_button.setEnabled(False)
            self.status.setText("중단 요청을 보냈습니다…")

    def last_assistant(self):
        for message in reversed(
            self.current_chat().get("messages", [])
        ):
            if (
                message.get("role") == "assistant"
                and message.get("text")
                and message.get("status") not in {
                    "failed", "cancelled", "pending"
                }
                and not message["text"].startswith("⚠ 요청 실패:")
            ):
                return message
        return None

    def export_text(self, title, name, text, file_filter):
        destination, _ = QFileDialog.getSaveFileName(
            self, title, name, file_filter
        )
        if destination:
            try:
                atomic_text(Path(destination), text)
                self.status.setText("파일을 저장했습니다.")
            except OSError as exc:
                QMessageBox.warning(self, "저장 오류", str(exc))

    def save_answer(self):
        message = self.last_assistant()
        if not message:
            QMessageBox.information(
                self, "답변 없음", "저장할 답변이 없습니다."
            )
            return

        self.export_text(
            "답변 저장", "answer.md", message["text"],
            "Markdown (*.md);;텍스트 (*.txt)",
        )

    def save_last_code(self):
        snippets = []

        for message in reversed(
            self.current_chat().get("messages", [])
        ):
            if (
                message.get("role") == "assistant"
                and message.get("status") not in {
                    "failed", "cancelled", "pending"
                }
            ):
                snippets = code_blocks(message.get("text", ""))
                if snippets:
                    break

        if not snippets:
            QMessageBox.information(
                self, "코드 없음", "저장할 코드 블록이 없습니다."
            )
            return

        index = 0
        if len(snippets) > 1:
            options = [
                f"{number + 1}. {language or 'text'} — "
                + body[:45].replace("\n", " ")
                for number, (language, body) in enumerate(snippets)
            ]
            chosen, accepted = QInputDialog.getItem(
                self, "코드 선택", "저장할 코드", options, 0, False
            )
            if not accepted:
                return
            index = options.index(chosen)

        language, code = snippets[index]
        extension = LANG_EXTENSIONS.get(language.lower(), ".txt")
        self.export_text(
            "코드 저장", "generated" + extension,
            code.rstrip() + "\n", "모든 파일 (*.*)",
        )

    def save_last_image(self):
        images = self.store.image_artifacts(self.current_chat())
        if not images:
            QMessageBox.information(
                self, "이미지 없음", "저장된 이미지가 없습니다."
            )
            return

        try:
            path = self.store.absolute(images[-1]["path"])
            if not path.is_file():
                raise FileNotFoundError("이미지 파일이 없습니다.")

            destination, _ = QFileDialog.getSaveFileName(
                self, "이미지 저장", path.name, "모든 파일 (*.*)"
            )

            if (
                destination
                and path.resolve() != Path(destination).resolve()
            ):
                shutil.copy2(path, destination)

        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "이미지 저장 실패", str(exc))

    def open_gallery(self):
        if self.chat_id:
            GalleryDialog(
                self, self.store, self.current_chat()
            ).exec()

    def open_chat_folder(self):
        if not self.chat_id:
            return

        try:
            directory = self.store.chat_dir(self.chat_id)
            directory.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(directory))
            ):
                raise OSError("폴더를 열지 못했습니다.")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "폴더 열기 실패", str(exc))

    def closeEvent(self, event):
        if self.generation_active() or self.auth_task is not None:
            QMessageBox.information(
                self, "요청 처리 중",
                "진행 중인 작업이 끝난 뒤 종료해 주세요. "
                "답변 생성은 '중단' 버튼으로 중단할 수 있습니다.",
            )
            event.ignore()
            return

        # 저장 실패로 메모리에만 남은 답변을 종료 전에 다시 저장합니다.
        if self.persist():
            event.accept()
        else:
            event.ignore()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Malgun Gothic", 10))
    app.setStyleSheet(APP_STYLE)

    try:
        window = MainWindow()
    except Exception as exc:
        QMessageBox.critical(
            None, "앱 시작 실패",
            "저장 폴더 또는 설정을 확인해 주세요.\n" + str(exc),
        )
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
