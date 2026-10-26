"""대화 저장, 첨부파일 검증 및 추출, 답변 보관 (v1.0.1)."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import credential_store

APP_VERSION = "1.0.8"
APP_DIR = Path.home() / "PastelAIStudio"

SUPPORTED_TEXT = {
    ".txt", ".md", ".csv", ".json", ".py", ".js", ".ts",
    ".jsx", ".tsx", ".vue", ".svelte",
    ".html", ".css", ".xml", ".yaml", ".yml", ".log", ".sql",
    ".java", ".cpp", ".c", ".h", ".cs", ".kt", ".swift", ".php",
    ".ipynb", ".sh", ".ps1", ".bat", ".cmd",
    ".ini", ".toml", ".rs", ".go", ".rb", ".r", ".lua", ".dart", ".scala",
}
SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_FILES = SUPPORTED_TEXT | SUPPORTED_IMAGES | {".pdf", ".docx"}

MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024
MAX_EXTRACTED_CHARS = 100_000
MAX_DOCX_UNCOMPRESSED_BYTES = 48 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_IMAGE_PIXELS = 24_000_000
MAX_CUSTOM_INSTRUCTION_CHARS = 4_000
DEFAULT_CONTEXT_MESSAGES = 16

CODE_RE = re.compile(
    r"^```([^\n`]*)\n(.*?)^```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

LANG_EXTENSIONS = {
    "python": ".py", "py": ".py",
    "javascript": ".js", "js": ".js",
    "jsx": ".jsx",
    "typescript": ".ts", "ts": ".ts",
    "tsx": ".tsx",
    "html": ".html", "css": ".css", "json": ".json",
    "cpp": ".cpp", "c++": ".cpp", "c": ".c", "h": ".h",
    "java": ".java", "kotlin": ".kt", "kt": ".kt",
    "swift": ".swift", "php": ".php",
    "sql": ".sql",
    "bash": ".sh", "sh": ".sh", "shell": ".sh",
    "powershell": ".ps1", "ps1": ".ps1",
    "batch": ".bat", "bat": ".bat", "cmd": ".cmd",
    "xml": ".xml", "yaml": ".yaml", "yml": ".yaml",
    "toml": ".toml", "ini": ".ini",
    "rust": ".rs", "rs": ".rs",
    "go": ".go", "golang": ".go",
    "ruby": ".rb", "rb": ".rb",
    "csharp": ".cs", "cs": ".cs",
    "r": ".r", "lua": ".lua", "dart": ".dart", "scala": ".scala",
    "markdown": ".md", "md": ".md",
    "text": ".txt", "plaintext": ".txt",
}


def new_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_")
    return stamp + uuid.uuid4().hex[:12]


def checked_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", value
    ):
        raise ValueError("올바르지 않은 저장 ID입니다.")
    return value


def code_language(value: str) -> str:
    words = value.strip().split()
    return words[0].lower() if words else ""


def code_blocks(text: str) -> list[tuple[str, str]]:
    return [
        (code_language(match.group(1)), match.group(2))
        for match in CODE_RE.finditer(text)
    ]


def split_code(text: str) -> list[tuple[str, str, str]]:
    result = []
    start = 0

    for match in CODE_RE.finditer(text):
        if match.start() > start:
            result.append(("prose", text[start:match.start()], ""))
        result.append((
            "code",
            match.group(2),
            code_language(match.group(1)),
        ))
        start = match.end()

    if start < len(text):
        result.append(("prose", text[start:], ""))

    return result


def atomic_text(path: Path, text: str) -> None:
    """같은 폴더의 고유 임시파일로 작성한 뒤 교체."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_attachment(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"파일을 찾을 수 없습니다: {path.name}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_FILES:
        raise ValueError(
            f"지원하지 않는 형식입니다: {suffix or path.name}"
        )

    if path.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"첨부파일은 각각 12MB 이하만 지원합니다: {path.name}"
        )

    if suffix in SUPPORTED_IMAGES:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise ValueError(
                    f"이미지는 2,400만 화소 이하만 지원합니다: {path.name}"
                )
            image.verify()


def collect_text(chunks, limit: int) -> tuple[str, bool]:
    parts = []
    remaining = limit

    for chunk in chunks:
        chunk = str(chunk)
        if parts:
            chunk = "\n" + chunk

        if len(chunk) > remaining:
            parts.append(chunk[:remaining])
            return "".join(parts), True

        parts.append(chunk)
        remaining -= len(chunk)

        if remaining == 0:
            return "".join(parts), True

    return "".join(parts), False


def read_attachment(
    path: Path,
    max_chars: int = MAX_EXTRACTED_CHARS,
) -> str:
    """텍스트 첨부의 본문만 반환. 이미지 경로는 별도로 전달."""
    validate_attachment(path)
    suffix = path.suffix.lower()
    limit = min(MAX_EXTRACTED_CHARS, max(1, max_chars))

    if suffix in SUPPORTED_IMAGES:
        raise ValueError("이미지는 텍스트로 읽을 수 없습니다.")

    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))

        if reader.is_encrypted:
            raise ValueError(f"암호화된 PDF는 지원하지 않습니다: {path.name}")

        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError(
                f"PDF는 최대 {MAX_PDF_PAGES}페이지까지 지원합니다."
            )

        # 전체 페이지의 추출 결과를 한 번에 연결하지 않습니다.
        extracted, truncated = collect_text(
            (page.extract_text() or "" for page in reader.pages),
            limit,
        )

        if not extracted.strip():
            raise ValueError(
                f"{path.name}: 텍스트가 없습니다. 스캔 PDF는 OCR이 필요합니다."
            )

    elif suffix == ".docx":
        from docx import Document

        # DOCX 압축 해제 크기를 먼저 제한합니다.
        with zipfile.ZipFile(path) as archive:
            expanded = sum(item.file_size for item in archive.infolist())
            if expanded > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ValueError(
                    f"압축 해제 크기가 너무 큰 DOCX입니다: {path.name}"
                )

        document = Document(str(path))

        def chunks():
            for paragraph in document.paragraphs:
                yield paragraph.text
            for table in document.tables:
                for row in table.rows:
                    yield " | ".join(cell.text for cell in row.cells)

        extracted, truncated = collect_text(chunks(), limit)

    else:
        try:
            with path.open(encoding="utf-8-sig") as handle:
                extracted = handle.read(limit + 1)
        except UnicodeDecodeError:
            with path.open(encoding="cp949", errors="replace") as handle:
                extracted = handle.read(limit + 1)

        truncated = len(extracted) > limit
        extracted = extracted[:limit]

    if truncated:
        extracted += "\n[파일 내용이 길어 일부만 전달되었습니다.]"

    return extracted


def search_chats(
    chats: dict[str, dict],
    query: str,
) -> list[tuple[str, dict]]:
    """대화 제목 및 메시지 본문에서 검색어와 일치하는 대화 목록을 최신순으로 반환."""
    keyword = query.strip().lower()
    items = list(reversed(list(chats.items())))
    if not keyword:
        return items

    matched = []
    for cid, chat in items:
        title = str(chat.get("title", "")).lower()
        if keyword in title:
            matched.append((cid, chat))
            continue

        for message in chat.get("messages", []):
            text = str(message.get("text", "")).lower()
            if keyword in text:
                matched.append((cid, chat))
                break

    return matched


def export_chat_markdown(chat: dict) -> str:
    """대화 전체를 읽기 좋은 Markdown 문서로 변환."""
    title = str(chat.get("title") or "대화")
    lines = [
        f"# {title}",
        "",
        f"- 내보낸 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 프로그램: Pastel AI Studio v{APP_VERSION}",
        "",
        "---",
        "",
    ]

    for message in chat.get("messages", []):
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue

        header = "## 🌸 나" if role == "user" else "## ✨ Pastel AI"
        lines.append(header)
        lines.append("")

        attachments = message.get("attachments", [])
        if attachments:
            names = ", ".join(
                str(item.get("name", "파일")) for item in attachments
            )
            lines.append(f"> 📎 첨부파일: {names}")
            lines.append("")

        body = str(message.get("text", "")).strip()
        lines.append(body or "_(내용 없음)_")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


class LocalStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or APP_DIR).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_file = self.root / "chats.json"
        self.settings_file = self.root / "settings.json"
        self.warnings: list[str] = []

    def absolute(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("허용되지 않은 저장 경로입니다.")
        return path

    def relative(self, path: Path) -> str:
        path = path.resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("앱 저장 폴더 바깥의 파일입니다.")
        return path.relative_to(self.root).as_posix()

    def _load_json(self, path: Path) -> dict:
        if not path.exists():
            return {}

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("최상위 데이터가 객체가 아닙니다.")
            return data
        except (OSError, ValueError) as exc:
            self.warnings.append(f"{path.name} 읽기 실패: {exc}")
            return {}

    def load_chats(self) -> dict[str, dict]:
        parsed = self._load_json(self.history_file)
        result = {}

        for cid, chat in parsed.items():
            try:
                checked_id(cid)
                if not isinstance(chat, dict):
                    raise ValueError("대화 형식 오류")

                raw_messages = chat.get("messages", [])
                if not isinstance(raw_messages, list):
                    raise ValueError("메시지 형식 오류")

                messages = []

                for raw in raw_messages:
                    if not isinstance(raw, dict):
                        continue
                    if raw.get("role") not in {"user", "assistant"}:
                        continue

                    message = dict(raw)
                    message["id"] = checked_id(
                        str(message.get("id") or new_id())
                    )
                    message["text"] = str(message.get("text", ""))

                    for field in ("attachments", "artifacts"):
                        values = message.get(field, [])
                        message[field] = [
                            item for item in values
                            if isinstance(item, dict)
                            and isinstance(item.get("path"), str)
                        ] if isinstance(values, list) else []

                    # 이전 실행이 종료된 경우 빈 대기를 복구합니다.
                    if (
                        message["role"] == "assistant"
                        and not message["text"]
                    ):
                        message["text"] = "⚠ 요청 실패: 이전 요청이 완료되지 않았습니다."
                        message["status"] = "failed"

                    messages.append(message)

                result[cid] = {
                    "title": str(chat.get("title", "대화")),
                    "messages": messages,
                }

            except ValueError as exc:
                self.warnings.append(f"대화 {cid} 읽기 실패: {exc}")

        return result

    def settings(self) -> dict:
        raw = self._load_json(self.settings_file)
        legacy_api_key = str(raw.get("api_key", "")).strip()
        provider = str(raw.get("provider", "codex")).strip().lower() or "codex"
        api_key = ""
        if provider in {"api", "claude"}:
            try:
                api_key = credential_store.get_api_key(provider)
                if legacy_api_key:
                    if not api_key:
                        credential_store.set_api_key(provider, legacy_api_key)
                        api_key = legacy_api_key
                    raw.pop("api_key", None)
                    atomic_text(self.settings_file, json.dumps(raw, ensure_ascii=False, indent=2))
            except credential_store.CredentialError as exc:
                raise ValueError(f"Windows 자격 증명 저장소 오류: {exc}") from exc
        try:
            context_limit = int(
                raw.get("max_context_messages", DEFAULT_CONTEXT_MESSAGES)
            )
        except (TypeError, ValueError):
            context_limit = DEFAULT_CONTEXT_MESSAGES

        return {
            "provider": provider,
            "model": str(raw.get("model", "")).strip(),
            "api_key": api_key,
            "api_base_url": str(raw.get("api_base_url", "https://api.openai.com/v1")).strip().rstrip("/"),
            "custom_instruction": str(
                raw.get("custom_instruction", "")
            ).strip()[:MAX_CUSTOM_INSTRUCTION_CHARS],
            "max_context_messages": max(2, min(64, context_limit)),
        }

    def save_chats(self, chats: dict) -> None:
        atomic_text(
            self.history_file,
            json.dumps(chats, ensure_ascii=False, indent=2),
        )

    def save_settings(self, settings: dict) -> None:
        try:
            context_limit = int(
                settings.get(
                    "max_context_messages", DEFAULT_CONTEXT_MESSAGES
                )
            )
        except (TypeError, ValueError):
            context_limit = DEFAULT_CONTEXT_MESSAGES

        provider = str(settings.get("provider", "codex")).strip().lower() or "codex"
        api_key = str(settings.get("api_key", "")).strip()
        if provider in {"api", "claude"}:
            credential_store.set_api_key(provider, api_key)
        payload = {
            "provider": provider,
            "model": str(settings.get("model", "")).strip(),
            "api_base_url": str(settings.get("api_base_url", "https://api.openai.com/v1")).strip().rstrip("/"),
            "custom_instruction": str(
                settings.get("custom_instruction", "")
            ).strip()[:MAX_CUSTOM_INSTRUCTION_CHARS],
            "max_context_messages": max(2, min(64, context_limit)),
        }
        atomic_text(
            self.settings_file,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def chat_dir(self, chat_id: str) -> Path:
        return self.absolute(f"chats/{checked_id(chat_id)}")

    def archive_attachment(
        self,
        chat_id: str,
        message_id: str,
        original: Path,
        index: int = 0,
    ) -> dict[str, str]:
        validate_attachment(original)
        checked_id(message_id)

        safe_name = re.sub(
            r"[^\w.()-]", "_", original.name, flags=re.UNICODE
        )[:110]

        directory = self.chat_dir(chat_id) / "attachments"
        destination = self.absolute(self.relative(
            directory / f"{message_id}_{index:02d}_{safe_name}"
        ))

        destination.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copy2(original, destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

        return {
            "name": original.name,
            "path": self.relative(destination),
        }

    def remove_attachments(self, attachments: list[dict]) -> None:
        for attachment in attachments:
            try:
                self.absolute(attachment["path"]).unlink(missing_ok=True)
            except ValueError:
                continue

    def archive_answer(self, chat_id: str, message: dict) -> list[dict]:
        mid = checked_id(message["id"])
        location = self.chat_dir(chat_id)
        artifacts = []
        message["artifacts"] = artifacts

        text = message.get("text", "")
        if not text:
            return artifacts

        def save(kind: str, path: Path, body: str, **extra):
            path = self.absolute(self.relative(path))
            atomic_text(path, body)
            artifacts.append({
                "kind": kind,
                "path": self.relative(path),
                "name": path.name,
                **extra,
            })

        save("answer", location / "answers" / f"{mid}.md", text)

        for index, (language, code) in enumerate(
            code_blocks(text), start=1
        ):
            extension = LANG_EXTENSIONS.get(language, ".txt")
            filename = f"{mid}_{index:02d}{extension}"
            save(
                "code",
                location / "code" / filename,
                code.rstrip() + "\n",
                language=language,
                downloadable=True,
            )

        return artifacts

    def image_artifacts(self, chat: dict) -> list[dict]:
        """대화에 첨부된 이미지와 생성된 이미지 결과물을 모두 순서대로 반환."""
        images: list[dict] = []
        seen: set[str] = set()

        for message in chat.get("messages", []):
            for attachment in message.get("attachments", []):
                rel_path = attachment.get("path")
                if not isinstance(rel_path, str) or rel_path in seen:
                    continue
                if Path(rel_path).suffix.lower() in SUPPORTED_IMAGES:
                    seen.add(rel_path)
                    images.append({
                        "kind": "image",
                        "path": rel_path,
                        "name": str(
                            attachment.get("name", Path(rel_path).name)
                        ),
                    })

            for artifact in message.get("artifacts", []):
                if artifact.get("kind") != "image":
                    continue
                rel_path = artifact.get("path")
                if not isinstance(rel_path, str) or rel_path in seen:
                    continue
                seen.add(rel_path)
                images.append(artifact)

        return images

    def delete_chat_files(self, chat_id: str) -> None:
        folder = self.chat_dir(chat_id)
        if folder.is_dir():
            shutil.rmtree(folder)
