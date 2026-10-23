from __future__ import annotations

import ast
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import wave
from queue import Empty, Queue
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Qt, QThread, Signal, Slot
from PySide6.QtGui import QFont, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QFileDialog, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSplitter, QStatusBar, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QHeaderView,
)

ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = Path(os.getenv("APPDATA", Path.home())) / "NeyroZetka"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "memory.db"
LOG_PATH = DATA_DIR / "crash.log"
WAKE_WORDS = tuple(bytes.fromhex(value).decode("utf-8") for value in (
    "d0b7d0b5d182d0b0",
    "d0b7d0b5d182d0bed187d0bad0b0",
    "d0bdd0b5d0b9d180d0bed0b7d0b5d182d0bad0b0",
    "d0b7d0b5d182d0bad0b0",
))
ADDRESS_WORDS = WAKE_WORDS + ("neyrozetka",)
GROQ_MODELS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"]
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.8-flash-live", "gemini-3.6-flash", "gemini-3.5-flash"]
VOICE_SAMPLE_SECONDS = 8.0
VOICE_SAMPLE_RATE = 16000
VOICE_FRAME_SECONDS = 0.25
VOICE_MIN_RMS = 180
VOICE_SILENCE_SECONDS = 1.0
VOICE_PREROLL_SECONDS = 0.5
IRC_PATTERN = re.compile(
    r"^(?:@[^ ]+\s+)?:(?P<author>[^!]+)!.*?PRIVMSG\s+#[^ ]+\s+:(?P<text>.*)$"
)


def parse_irc_message(line: str) -> tuple[str, str] | None:
    """Parse Twitch PRIVMSG with or without IRCv3 tags."""
    match = IRC_PATTERN.match(line.strip())
    if not match:
        return None
    return match.group("author").strip(), match.group("text")


def is_addressed(text: str) -> bool:
    normalized = text.casefold()
    return any(re.search(rf"(?<!\w){re.escape(word.casefold())}(?!\w)", normalized)
               for word in ADDRESS_WORDS)


def record_voice_utterance(sd: Any, np: Any, device: int, stop_event: threading.Event, sensitivity: float = 1.8, noise_floor: float = 0.0, level_callback: Any = None) -> Any:
    """Record one natural utterance, preserving a short pre-roll and trailing pause."""
    frame_size = int(VOICE_SAMPLE_RATE * VOICE_FRAME_SECONDS)
    pre_roll: list[Any] = []
    frames: list[Any] = []
    pre_roll_limit = max(1, int(VOICE_PREROLL_SECONDS / VOICE_FRAME_SECONDS))
    max_frames = max(1, int(VOICE_SAMPLE_SECONDS / VOICE_FRAME_SECONDS))
    silence_frames = max(1, int(VOICE_SILENCE_SECONDS / VOICE_FRAME_SECONDS))
    noise_samples: list[float] = []
    speaking = False
    quiet_count = 0
    with sd.InputStream(
        samplerate=VOICE_SAMPLE_RATE,
        blocksize=frame_size,
        channels=1,
        dtype="int16",
        device=device,
    ) as stream:
        for _ in range(max_frames * 2):
            if stop_event.is_set():
                return None
            frame, _ = stream.read(frame_size)
            samples = np.asarray(frame[:, 0], dtype=np.int16).copy()
            rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32))) + 1.0))
            if level_callback:
                level_callback(min(1.0, rms / 3000.0))
            if not speaking:
                noise_samples.append(rms)
                baseline = max(30.0, float(np.median(noise_samples[-8:])))
                baseline = max(baseline, noise_floor)
                threshold = max(float(VOICE_MIN_RMS) * sensitivity / 1.8, baseline * sensitivity)
                pre_roll.append(samples)
                if len(pre_roll) > pre_roll_limit:
                    pre_roll.pop(0)
                if rms >= threshold:
                    speaking = True
                    frames.extend(pre_roll)
                    quiet_count = 0
                continue
            frames.append(samples)
            baseline = max(30.0, float(np.median(noise_samples[-8:] or [30.0])))
            baseline = max(baseline, noise_floor)
            threshold = max(float(VOICE_MIN_RMS) * sensitivity / 1.8, baseline * sensitivity * 0.9)
            quiet_count = quiet_count + 1 if rms < threshold else 0
            if quiet_count >= silence_frames or len(frames) >= max_frames:
                break
    if not frames:
        return None
    audio = np.concatenate(frames).astype(np.float32)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0:
        audio = audio * min(1.8, 30000.0 / peak)
    return np.clip(audio, -32768, 32767).astype(np.int16)

DEFAULT_CONFIG = {
    "twitch": {"channel": "s_mirt_y", "streamer_token": "", "bot_login": "neyrozetka", "bot_oauth_token": ""},
    "obs": {"host": "127.0.0.1", "port": 4455, "password": "", "scene": "", "image_source": "NeyroZetka", "character_image": "", "emotion_idle": "", "emotion_talking": "", "emotion_laugh": "", "emotion_angry": "", "emotion_error": "", "character_duration_sec": 5, "auto_scene": True, "auto_image_source": True},
    "llm": {
        "active_provider": "groq", "groq_api_key": "", "groq_model": GROQ_MODELS[0],
        "gemini_api_key": "", "gemini_model": GEMINI_MODELS[0],
        "system_prompt": "Ты — НейроЗетка, дерзкая и живая AI-соведущая Twitch. Отвечай коротко, естественно и по делу, идеально для голосового воспроизведения TTS. Используй лёгкий юмор, дружелюбный тон и не перегружай канцелярскими вводными.\n\nПравила:\n- Анализируй общую семантику и контекст голосовой фразы, а не только буквальное написание. Исправляй опечатки, потерянные знаки препинания, фонетические искажения и шум без лишних уточнений.\n- Если фраза неполная или расплывчатая, интерпретируй её по текущей теме общения, не заставляя пользователя повторять вопрос без необходимости.\n- Отвечай строго на последнее сообщение или актуальный текущий вопрос. Не возвращайся к темам, которые были давно и уже ушли из текущего контекста.\n- Если вопрос или просьба не была поднята в последних 5–10 сообщениях, считай её пройденным контекстом и не поднимай её без явного запроса.\n- Факты о пользователе, привычки, шутки, особенности общения и «inside jokes» запоминай как часть адаптивной памяти; задачи, вопросы и указания сгорают после смены темы.\n- Используй персональные детали только для тонкой адаптации, уместных шуток и естественных отсылок, но не для навязчивого влияния на диалог.\n- Ответ должен быть коротким, живым и понятным, максимум 1–3 коротких предложения, иногда с лёгкой шуткой.\n- Не теряй смысл, не повторяй чужие слова, не задавай лишних встречных вопросов и не вытягивай разговор без необходимости.",
    },
    "vision_audio": {"obs_source": "Game Capture", "capture_period_sec": 60, "wake_words": list(WAKE_WORDS), "microphone_name": "Микрофон (fifine Microphone)", "microphone_sensitivity": 1.8, "microphone_noise_floor": 0.0},
}

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
    encoding="utf-8",
)
LOGGER = logging.getLogger("neyrozetka")


def log_unhandled(exc_type: type[BaseException], exc_value: BaseException, exc_traceback: Any) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    LOGGER.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))


def log_thread_exception(args: threading.ExceptHookArgs) -> None:
    LOGGER.critical("Unhandled worker exception", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


sys.excepthook = log_unhandled
if hasattr(threading, "excepthook"):
    threading.excepthook = log_thread_exception


def clean(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value


class ConfigManager:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            self.path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
            return json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        result = self._merge(DEFAULT_CONFIG, clean(loaded))
        if result["llm"].get("groq_model") not in GROQ_MODELS + ["openai/gpt-oss-120b"]:
            result["llm"]["groq_model"] = "openai/gpt-oss-120b"
        if result["llm"].get("gemini_model") not in GEMINI_MODELS + ["gemini-3.8-flash"]:
            result["llm"]["gemini_model"] = "gemini-3.8-flash"
        return result

    def _merge(self, defaults: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for key, default in defaults.items():
            value = values.get(key, default)
            result[key] = self._merge(default, value) if isinstance(default, dict) else value
        return result

    def get(self, section: str, key: str, default: Any = "") -> Any:
        value = self.data.get(section, {}).get(key, default)
        return None if isinstance(value, str) and not value.strip() and ("key" in key or "token" in key or key == "password") else value

    def set(self, section: str, key: str, value: Any) -> None:
        self.data.setdefault(section, {})[key] = clean(value)

    def save(self) -> None:
        self.data = clean(self.data)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path, self.lock = path, threading.RLock()
        with self.connect() as con:
            con.executescript("""CREATE TABLE IF NOT EXISTS chat_memory(
                id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT NOT NULL, content TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS user_profiles(
                author TEXT PRIMARY KEY, relationship TEXT DEFAULT 'Нейтральный',
                score INTEGER DEFAULT 0, facts TEXT DEFAULT '', updated_at TEXT DEFAULT CURRENT_TIMESTAMP);""")

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def add(self, author: str, content: str) -> None:
        with self.lock, self.connect() as con:
            con.execute("INSERT INTO chat_memory(author,content) VALUES(?,?)", (author.strip(), content.strip()))

    def recent(self, author: str) -> list[sqlite3.Row]:
        with self.lock, self.connect() as con:
            return con.execute("SELECT * FROM chat_memory WHERE author=? ORDER BY id DESC LIMIT 12", (author,)).fetchall()

    def recent_global(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.lock, self.connect() as con:
            return con.execute("SELECT * FROM chat_memory ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def remember_fact(self, author: str, fact: str) -> None:
        fact = fact.strip()
        if not fact:
            return
        with self.lock, self.connect() as con:
            row = con.execute("SELECT facts FROM user_profiles WHERE author=?", (author,)).fetchone()
            facts = [item.strip() for item in str(row["facts"] if row else "").splitlines() if item.strip()]
            if fact.casefold() not in {item.casefold() for item in facts}:
                facts.append(fact)
            facts = facts[-50:]
            con.execute(
                "INSERT INTO user_profiles(author, facts, updated_at) VALUES(?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(author) DO UPDATE SET facts=excluded.facts, updated_at=CURRENT_TIMESTAMP",
                (author.strip(), "\n".join(facts)),
            )

    def profile_context(self, author: str) -> str:
        with self.lock, self.connect() as con:
            row = con.execute("SELECT facts FROM user_profiles WHERE author=?", (author,)).fetchone()
        facts = str(row["facts"] if row else "").strip()
        return f"Постоянные факты о пользователе:\n{facts}" if facts else ""

    def all(self) -> list[sqlite3.Row]:
        with self.lock, self.connect() as con:
            return con.execute("SELECT * FROM chat_memory ORDER BY id DESC LIMIT 500").fetchall()

    def delete(self, row_id: int) -> None:
        with self.lock, self.connect() as con:
            con.execute("DELETE FROM chat_memory WHERE id=?", (row_id,))

    def backup(self) -> Path:
        target = self.path.with_name(f"memory-backup-{time.strftime('%Y%m%d-%H%M%S')}.db")
        with self.lock, self.connect() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        return target


class Events(QObject):
    status = Signal(str, bool, str)
    chat = Signal(str, str)
    display = Signal(str, str)
    speech = Signal(str)
    log = Signal(str, str)
    toast = Signal(str)


def request_json(url: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: int = 90) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode()
    request_headers = {
        "User-Agent": "NeyroZetka/1.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
    } | (headers or {})
    request = urllib.request.Request(url, data=body, headers=request_headers)
    LOGGER.info("HTTP request %s timeout=%ss", url, timeout)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        raise RuntimeError(f"Сервер {url} не ответил вовремя ({timeout}s): {exc}") from exc


def request_stream_text(url: str, payload: dict[str, Any], headers: dict[str, str]) -> str:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, headers={
        "User-Agent": "NeyroZetka/1.0",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    } | headers)
    parts: list[str] = []
    LOGGER.info("Streaming HTTP request %s model=%s", url, payload.get("model"))
    try:
        response_context = urllib.request.urlopen(request, timeout=90)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        raise RuntimeError(f"Сервер {url} не ответил вовремя (90s): {exc}") from exc
    with response_context as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", "ignore").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            try:
                chunk = json.loads(line[5:].strip())
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta:
                    parts.append(delta)
            except json.JSONDecodeError:
                continue
    answer = "".join(parts).strip()
    if not answer:
        raise RuntimeError("Groq вернул пустой поток ответа")
    return answer


def list_groq_text_models(api_key: str) -> list[str]:
    data = request_json("https://api.groq.com/openai/v1/models", None, {"Authorization": "Bearer " + api_key})
    models: list[str] = []
    for item in data.get("data", []):
        model_id = str(item.get("id", "")).strip()
        if not model_id:
            continue
        lowered = model_id.lower()
        if any(token in lowered for token in ("whisper", "audio", "tts", "speech", "vision", "image", "transcription", "embed", "live")):
            continue
        models.append(model_id)
    unique = sorted(set(models), key=lambda value: value.lower())
    if not unique:
        raise RuntimeError("Groq не вернул доступных текстовых моделей")
    return unique


def list_gemini_text_models(api_key: str) -> list[str]:
    data = request_json(f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}")
    models: list[str] = []
    for item in data.get("models", []):
        model_id = str(item.get("name", "")).replace("models/", "").strip()
        if not model_id:
            continue
        lowered = model_id.lower()
        if any(token in lowered for token in ("embed", "embedding", "tts", "audio", "speech", "voice", "transcription", "image", "vision", "video", "live")):
            continue
        models.append(model_id)
    unique = sorted(set(models), key=lambda value: value.lower())
    if not unique:
        raise RuntimeError("Gemini не вернул доступных текстовых моделей")
    return unique


def transcribe_wav(path: Path, api_key: str) -> str:
    boundary = "----NeyroZetkaBoundary"
    audio = path.read_bytes()
    fields = [
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.wav\"\r\n"
         "Content-Type: audio/wav\r\n\r\n").encode() + audio,
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n"
         "whisper-large-v3-turbo").encode(),
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nru").encode(),
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\n"
         "Это голосовая команда стримера на русском языке. Возможные обращения: "
         "НейроЗетка, Зеточка, Зетка, нейрозетка. Сохраняй смысл и не додумывай слова.").encode(),
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"temperature\"\r\n\r\n0").encode(),
    ]
    body = b"\r\n".join(fields) + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": "Bearer " + api_key,
            "User-Agent": "NeyroZetka/1.0",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))
    return str(result.get("text", "")).strip()


class LLMService:
    def __init__(self, config: ConfigManager, memory: MemoryStore, events: Events) -> None:
        self.config, self.memory, self.events = config, memory, events

    def complete(self, text: str, author: str, session_context: str = "") -> str:
        LOGGER.info("LLM request provider=%s author=%s", self.config.get("llm", "active_provider", "groq"), author)
        provider = self.config.get("llm", "active_provider", "groq")
        if provider == "auto":
            providers = ["groq", "gemini"]
        else:
            providers = [provider]
        for selected in providers:
            key_name = f"{selected}_api_key"
            api_key = self.config.get("llm", key_name)
            if not api_key:
                LOGGER.error("Missing %s API key", selected)
                self.events.status.emit("LLM", False, f"Ошибка: Не задан API-ключ для {selected.title()}")
                if provider != "auto":
                    return f"Ошибка: Не задан API-ключ для {selected.title()}"
                continue
            try:
                recent = "\n".join(row["content"] for row in reversed(self.memory.recent(author)))
                chat = "\n".join(f"{row['author']}: {row['content']}" for row in reversed(self.memory.recent_global()))
                profile = self.memory.profile_context(author)
                context = "\n".join(item for item in (
                    session_context,
                    "Последний контекст общего чата:\n" + chat if chat else "",
                    profile,
                    "История общения с этим пользователем:\n" + recent if recent else "",
                ) if item)
                result = self._call_with_retry(selected, api_key, context, text)
                answer = result[:250].strip()
                self.memory.add("НейроЗетка", answer)
                self.events.status.emit("LLM", True, f"{selected.title()} готов")
                return answer
            except urllib.error.HTTPError as exc:
                LOGGER.error("LLM HTTP %s provider=%s", exc.code, selected)
                if exc.code == 403:
                    message = f"[{selected.title()} Error] Доступ запрещен (403). Проверьте валидность API-ключа или региональные ограничения"
                else:
                    message = f"[{selected.title()} Error] HTTP {exc.code}"
                self.events.log.emit(message, "error")
                self.events.status.emit("LLM", False, message)
                if provider != "auto":
                    return message
            except Exception as exc:
                LOGGER.exception("LLM request failed provider=%s", selected)
                self.events.log.emit(f"[{selected.title()} Error] {exc}", "error")
                if provider != "auto":
                    return f"Ошибка {selected.title()}: {exc}"
        return "Ошибка: не удалось получить ответ от настроенного провайдера"

    def _call_with_retry(self, provider: str, key: str, context: str, text: str) -> str:
        for attempt in range(2):
            try:
                return self._call(provider, key, context, text)
            except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
                if attempt == 0:
                    LOGGER.warning("Temporary %s failure, retrying: %s", provider, exc)
                    time.sleep(0.4)
                    continue
                raise
        raise RuntimeError("Провайдер не ответил после повторной попытки")

    def _call(self, provider: str, key: str, context: str, text: str) -> str:
        prompt = self.config.get("llm", "system_prompt", "")
        style = self.response_style(context, text)
        directive = (
            "\nРежим ответа сейчас: " + style + ". "
            "Подкалывай только доброжелательно и только если собеседник явно в лёгком настроении. "
            "Не шути над болью, злостью, ошибками, внешностью или личными проблемами. "
            "Если смысл неоднозначен, выбери уважительный короткий ответ без выдуманных фактов."
        )
        if provider == "groq":
            return request_stream_text("https://api.groq.com/openai/v1/chat/completions", {
                "model": self.config.get("llm", "groq_model", GROQ_MODELS[0]),
                "messages": [
                    {"role": "system", "content": prompt + directive + "\n"
                     "Отвечай на вопрос пользователя, не повторяй его и не задавай встречный вопрос. "
                     "Нужен короткий готовый ответ для Twitch-чата."},
                    {"role": "user", "content": f"{context}\n{text}"},
                ],
                "temperature": 1,
                "max_completion_tokens": 2048,
                "top_p": 1,
                "reasoning_effort": "medium",
                "stream": True,
                "stop": None,
            }, {"Authorization": "Bearer " + key})
        data = request_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.config.get('llm', 'gemini_model', GEMINI_MODELS[0])}:generateContent?key={key}",
            {"contents": [{"parts": [{"text": f"{prompt}{directive}\n{context}\n{text}"}]}]},
            timeout=90,
        )
        return data["candidates"][0]["content"]["parts"][0]["text"]

    @staticmethod
    def response_style(context: str, text: str) -> str:
        combined = f"{context}\n{text}".casefold()
        if any(token in combined for token in ("злое", "злюсь", "бесит", "грустное", "печально")):
            return "спокойный, поддерживающий и аккуратный"
        if any(token in combined for token in ("ошибка", "как настроить", "помоги", "почему не", "не работает")):
            return "ясный, практичный и без шуток"
        if any(token in combined for token in ("весёлое", "радостное", "хаха", "лол", "кек", "😂")):
            return "живой, игривый и с мягкой доброй подколкой"
        return "естественный, дружелюбный и слегка ироничный"

    def validate(self, provider: str) -> str:
        key = self.config.get("llm", f"{provider}_api_key")
        if not key:
            raise ValueError(f"Не задан API-ключ для {provider.title()}")
        if provider == "groq":
            request_json("https://api.groq.com/openai/v1/models", None, {"Authorization": "Bearer " + key})
        else:
            request_json(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}")
        return provider


class NetworkTask(QThread):
    finished = Signal(bool, str)

    def __init__(self, work: Any) -> None:
        super().__init__(); self.work = work

    def run(self) -> None:
        try:
            self.finished.emit(True, str(self.work()))
        except Exception as exc:
            LOGGER.exception("Background network task failed")
            self.finished.emit(False, str(exc))


class TwitchWorker(QThread):
    message, state, viewer_joined = Signal(str, str), Signal(bool, str), Signal(str)

    def __init__(self, config: ConfigManager) -> None:
        super().__init__(); self.config, self.stop_event = config, threading.Event(); self.outbox: Queue[str] = Queue()

    def stop(self) -> None: self.stop_event.set()

    def send(self, text: str) -> None:
        text = re.sub(r"[\r\n]+", " ", text).strip()[:450]
        if text:
            self.outbox.put(text)

    def run(self) -> None:
        LOGGER.info("Twitch worker started")
        while not self.stop_event.is_set():
            nick = self.config.get("twitch", "bot_login")
            token = self.config.get("twitch", "bot_oauth_token")
            channel = self.config.get("twitch", "channel")
            if not nick or not token or not channel:
                self.state.emit(False, "Не заполнены Twitch Bot Login/Token/Channel"); self.stop_event.wait(4); continue
            sock = None
            try:
                sock = socket.create_connection(("irc.chat.twitch.tv", 6667), 15); sock.settimeout(1)
                for line in (f"PASS oauth:{token.removeprefix('oauth:')}", f"NICK {nick}", "CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership", f"JOIN #{channel.lstrip('#')}"):
                    sock.sendall((line + "\r\n").encode())
                self.state.emit(True, "Twitch IRC подключен"); buffer = b""
                while not self.stop_event.is_set():
                    while True:
                        try:
                            outgoing = self.outbox.get_nowait()
                        except Empty:
                            break
                        sock.sendall(f"PRIVMSG #{channel.lstrip('#')} :{outgoing}\r\n".encode())
                    try: buffer += sock.recv(4096)
                    except socket.timeout: continue
                    while b"\r\n" in buffer:
                        raw, buffer = buffer.split(b"\r\n", 1); line = raw.decode("utf-8", "ignore")
                        if line.startswith("PING"):
                            sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                            continue
                        if " 001 " in line or " 376 " in line:
                            self.state.emit(True, "Twitch IRC authorized and listening")
                            continue
                        if " 401 " in line or (" NOTICE " in line and "Login authentication failed" in line):
                            LOGGER.error("Twitch IRC authentication rejected")
                            self.state.emit(False, "Twitch OAuth rejected: check token, login and chat scopes")
                            self.stop_event.set()
                            break
                        if " JOIN #" in line and line.startswith(":"):
                            self.viewer_joined.emit(line[1:].split("!", 1)[0])
                        parsed = parse_irc_message(line)
                        if parsed:
                            self.message.emit(*parsed)
            except ConnectionRefusedError:
                self.state.emit(False, "Twitch IRC соединение отклонено")
                self.stop_event.wait(5)
            except Exception as exc:
                LOGGER.exception("Twitch worker connection failed")
                self.state.emit(False, f"Twitch reconnect: {exc}"); self.stop_event.wait(5)
            finally:
                if sock: sock.close()


class VoiceWorker(QThread):
    transcript = Signal(str)
    state = Signal(str, bool, str)
    error = Signal(str)
    level = Signal(float)

    def __init__(self, config: ConfigManager, device_index: int, events: Events) -> None:
        super().__init__()
        self.config = config
        self.device_index = device_index
        self.events = events
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd
            while not self.stop_event.is_set():
                self.events.log.emit("Ожидаю речь...", "info")
                sensitivity = float(self.config.get("vision_audio", "microphone_sensitivity", 1.8) or 1.8)
                noise_floor = float(self.config.get("vision_audio", "microphone_noise_floor", 0.0) or 0.0)
                samples = record_voice_utterance(sd, np, self.device_index, self.stop_event, sensitivity, noise_floor, self.level.emit)
                if samples is None:
                    continue
                self.events.log.emit(f"Фраза записана ({len(samples) / VOICE_SAMPLE_RATE:.1f} сек.), распознаю...", "info")
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as file:
                    wav_path = Path(file.name)
                try:
                    with wave.open(str(wav_path), "wb") as wav:
                        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(VOICE_SAMPLE_RATE)
                        wav.writeframes(samples.tobytes())
                    text = transcribe_wav(wav_path, str(self.config.get("llm", "groq_api_key")))
                finally:
                    wav_path.unlink(missing_ok=True)
                if not text:
                    self.events.log.emit("Речь не распознана", "info")
                    continue
                self.transcript.emit(text)
        except Exception as exc:
            LOGGER.exception("Voice STT failed")
            self.error.emit(f"Ошибка распознавания: {exc}")
        finally:
            self.state.emit("STT", False, "Слушание остановлено")


class OBSWorker(QThread):
    frame, state = Signal(bytes), Signal(bool, str)

    def __init__(self, config: ConfigManager) -> None:
        super().__init__(); self.config, self.stop_event = config, threading.Event()

    def stop(self) -> None: self.stop_event.set()

    def capture_frame(self, client: Any, source_name: str) -> None:
        import base64
        result = client.get_source_screenshot(source_name, "jpg", 640, 360, 75)
        self.frame.emit(base64.b64decode(result.image_data.split(",", 1)[-1]))

    def run(self) -> None:
        LOGGER.info("OBS worker started")
        try:
            import base64
            import obsws_python as obs
            client = obs.ReqClient(
                host=self.config.get("obs", "host", "127.0.0.1"),
                port=int(self.config.get("obs", "port", 4455)),
                password=self.config.get("obs", "password"),
                timeout=5,
            )
            source_name = str(self.config.get("vision_audio", "obs_source", "Game Capture")).strip()
            if not source_name:
                raise ValueError("Не указан OBS Source")
            self.state.emit(True, "OBS WebSocket подключен")
            self.capture_frame(client, source_name)
            period = max(5, int(self.config.get("vision_audio", "capture_period_sec", 60)))
            while not self.stop_event.wait(period):
                self.capture_frame(client, source_name)
        except ConnectionRefusedError:
            self.state.emit(False, "OBS выключен или WebSocket не включен в OBS Studio (порт 4455)")
        except Exception as exc:
            LOGGER.exception("OBS worker failed")
            self.state.emit(False, f"OBS: не удалось получить источник «{self.config.get('vision_audio', 'obs_source', '')}»: {exc}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config, self.memory_store, self.events = ConfigManager(), MemoryStore(DB_PATH), Events()
        self.last_frame_at = 0.0
        self.voice_stop = threading.Event()
        self.voice_thread: VoiceWorker | None = None
        self.voice_answer_lock = threading.Lock()
        self.model_tasks: list[NetworkTask] = []
        self.stream_started_at: float | None = None
        self.viewer_seen_at: dict[str, float] = {}
        self.stream_memory: list[tuple[str, str]] = []
        self.active_viewers: set[str] = set()
        self.stream_stats = {"messages": 0, "answers": 0, "started": None}
        self.chat_mood = "Спокойное"
        self.chat_emotion = "idle"
        self.emotion_intensity = 0.0
        self.bot_mood = "Спокойное"
        self.bot_emotion = "idle"
        self.bot_emotion_intensity = 0.0
        self.last_answer_by_author: dict[str, tuple[float, str]] = {}
        self.last_answer_text = ""
        self.last_answer_at = 0.0
        self.answer_cooldown_seconds = 4.0
        self.twitch: TwitchWorker | None = None; self.obs: OBSWorker | None = None; self.bot_running = False
        self.setWindowTitle("НейроЗетка — AI Co-host"); self.resize(1180, 760); self.setMinimumSize(720, 520)
        character_image = str(self.config.get("obs", "character_image", "") or "")
        if character_image and Path(character_image).exists():
            self.setWindowIcon(QIcon(character_image))
        self.setStyleSheet("""
        QWidget{background:#0f121b;color:#edf0f7;font-size:13px}
        QMainWindow{background:#0f121b}
        QScrollArea{background:#0f121b;border:0}
        QGroupBox{background:#171c29;border:1px solid #303b59;border-radius:12px;margin-top:16px;padding:16px}
        QGroupBox::title{subcontrol-origin:margin;left:14px;padding:0 8px;color:#8fe7ff;font-weight:700}
        QLineEdit,QPlainTextEdit,QComboBox{background:#090c13;border:1px solid #35415f;border-radius:8px;padding:8px;color:#f2f4fa;selection-background-color:#267e99;min-height:18px}
        QLineEdit:focus,QPlainTextEdit:focus,QComboBox:focus{border:1px solid #63d8f2;background:#0c121c}
        QPushButton{background:#1e7189;border:1px solid #2e9db8;border-radius:8px;padding:9px 15px;min-height:18px;font-weight:700;color:#f7fcff}
        QPushButton:hover{background:#2895ad;border-color:#7deaff}
        QPushButton:pressed{background:#15576b}
        QTabWidget::pane{border:1px solid #293650;border-radius:10px;background:#121722;top:-1px}
        QTabBar::tab{background:#151b29;color:#9ca9c2;padding:12px 20px;margin-right:4px;border-top-left-radius:8px;border-top-right-radius:8px}
        QTabBar::tab:selected{background:#1e7189;color:#ffffff;border-bottom:2px solid #8fe7ff}
        QTabBar::tab:hover{background:#223149;color:#ffffff}
        QToolTip{background:#202b40;color:#ffffff;border:1px solid #63d8f2;padding:7px}
        QTableWidget{background:#0c1018;alternate-background-color:#141b28;gridline-color:#283650;border:1px solid #303b59;border-radius:8px}
        QTableWidget::item:selected{background:#1e7189;color:#ffffff}
        QHeaderView::section{background:#1e293c;color:#d9e7f0;border:0;padding:8px;font-weight:700}
        QProgressBar{background:#090c13;border:1px solid #35415f;border-radius:7px;text-align:center;color:#edf0f7;height:18px}
        QProgressBar::chunk{background:#2e9db8;border-radius:6px}
        QScrollBar:vertical{background:#111725;width:10px;margin:2px}
        QScrollBar::handle:vertical{background:#3c5772;border-radius:5px;min-height:25px}
        QScrollBar::handle:vertical:hover{background:#63d8f2}
        QStatusBar{background:#090c13;color:#9ca9c2;border-top:1px solid #293650}
        QListWidget#sidebar{background:#0a0f18;border:0;border-right:1px solid #1d2a40;padding:10px 8px}
        QListWidget#sidebar::item{color:#9ca9c2;padding:12px 10px;border-radius:8px;margin:2px 0}
        QListWidget#sidebar::item:hover{background:#17263a;color:#ffffff}
        QListWidget#sidebar::item:selected{background:#1e7189;color:#ffffff;font-weight:800}
        """)
        self.build(); self.bind()

    def line(self, section: str, key: str, secret: bool = False) -> QLineEdit:
        item = QLineEdit("" if self.config.get(section, key) is None else str(self.config.get(section, key)))
        if secret: item.setEchoMode(QLineEdit.EchoMode.Password)
        def save_text(value: str) -> None:
            self.config.set(section, key, value.strip())
        item.textChanged.connect(save_text)
        if section == "llm" and key == "groq_api_key":
            item.editingFinished.connect(lambda: self.refresh_groq_models(silent=True))
        elif section == "llm" and key == "gemini_api_key":
            item.editingFinished.connect(lambda: self.refresh_gemini_models(silent=True))
        return item

    def build(self) -> None:
        central, root = QWidget(), QVBoxLayout()
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)
        header = QHBoxLayout()
        header.setSpacing(14)
        title = QLabel("НЕЙРОЗЕТКА")
        title.setStyleSheet("font-size:25px;font-weight:900;color:#8fe7ff;letter-spacing:1px")
        subtitle = QLabel("AI co-host  •  Twitch control center")
        subtitle.setStyleSheet("color:#9ca9c2;font-size:12px;padding-top:8px")
        header.addWidget(title)
        header.addWidget(subtitle)
        header.addStretch()
        self.live_indicator = QLabel("● ГОТОВА К РАБОТЕ")
        self.live_indicator.setStyleSheet("color:#9ff5c5;font-weight:800;padding:8px 14px;background:#123629;border:1px solid #2c9c70;border-radius:9px")
        header.addWidget(self.live_indicator)
        root.addLayout(header)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.scroll_page(self.overview()), "Главная")
        self.tabs.addTab(self.scroll_page(self.connections()), "Twitch")
        self.tabs.addTab(self.scroll_page(self.obs_connections()), "OBS")
        self.tabs.addTab(self.scroll_page(self.ai()), "\u0418\u0418 \u0438 \u043c\u043e\u0434\u0435\u043b\u0438")
        self.tabs.addTab(self.scroll_page(self.vision()), "\u041c\u0438\u043a\u0440\u043e\u0444\u043e\u043d")
        self.tabs.addTab(self.chat(), "\u0416\u0438\u0432\u043e\u0439 \u0447\u0430\u0442")
        self.tabs.addTab(self.memory(), "\u041f\u0430\u043c\u044f\u0442\u044c")
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.tabBar().hide()
        body = QHBoxLayout()
        body.setSpacing(0)
        sidebar = self.build_sidebar()
        body.addWidget(sidebar)
        body.addWidget(self.tabs, 1)
        root.addLayout(body, 1)
        footer = QVBoxLayout()
        actions = QHBoxLayout()
        self.start_button = QPushButton("\u0417\u0410\u041f\u0423\u0421\u0422\u0418\u0422\u042c \u0411\u041e\u0422\u0410")
        self.start_button.setStyleSheet("background:#2ea44f;font-weight:bold")
        self.start_button.clicked.connect(self.toggle_bot)
        save = QPushButton("\u0421\u041e\u0425\u0420\u0410\u041d\u0418\u0422\u042c \u041d\u0410\u0421\u0422\u0420\u041e\u0419\u041a\u0418")
        save.clicked.connect(self.save_config)
        logs = QPushButton("\u041e\u0422\u041a\u0420\u042b\u0422\u042c \u041b\u041e\u0413")
        logs.clicked.connect(self.open_log)
        for button in (self.start_button, save, logs): actions.addWidget(button)
        actions.addStretch()
        self.status_panel = QLabel("Состояние: готово")
        self.status_panel.setStyleSheet(
            "background:#18253a;border:1px solid #3c4b73;border-radius:10px;"
            "padding:8px 12px;color:#dfe7ff;font-weight:600"
        )
        actions.addWidget(self.status_panel)
        footer.addLayout(actions)
        status_row = QHBoxLayout()
        self.badges = {}
        for name in ("Twitch", "OBS", "LLM", "STT"):
            badge = QLabel(f"{name}: \u25cf \u041d\u0435 \u043f\u0440\u043e\0432\u0435\u0440\u0435\u043d")
            badge.setStyleSheet(
                "background:#1c2334;border:1px solid #394765;border-radius:8px;"
                "padding:6px 10px;color:#f0ad4e;font-weight:600"
            )
            self.badges[name] = badge
            badge.setWordWrap(True)
            badge.setMinimumWidth(120)
            badge.setMaximumWidth(260)
            status_row.addWidget(badge)
        footer.addLayout(status_row)
        root.addLayout(footer)
        central.setLayout(root)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(f"\u041b\u043e\u0433: {LOG_PATH}")

    def build_sidebar(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(205)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        brand = QLabel("⚡ НЕЙРОЗЕТКА")
        brand.setStyleSheet("font-size:17px;font-weight:900;color:#8fe7ff;padding:10px 8px 0")
        layout.addWidget(brand)
        subtitle = QLabel("AI CO-HOST\nTWITCH CONTROL CENTER")
        subtitle.setStyleSheet("font-size:9px;color:#647792;padding:0 8px 12px;letter-spacing:1px")
        layout.addWidget(subtitle)
        self.sidebar = QListWidget(objectName="sidebar")
        self.sidebar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        items = ("⌂  Главная", "◈  Twitch", "◉  OBS Studio", "✦  ИИ и модели", "♩  Микрофон и голос", "▤  Живой чат", "▣  Память и логи")
        for item in items:
            self.sidebar.addItem(QListWidgetItem(item))
        self.sidebar.currentRowChanged.connect(self.tabs.setCurrentIndex)
        self.sidebar.setCurrentRow(0)
        layout.addWidget(self.sidebar, 1)
        profile = QLabel("●  НейроЗетка\n\n   AI co-host\n   Система готова")
        profile.setStyleSheet("background:#121c2b;border:1px solid #2b405c;border-radius:10px;padding:12px;color:#dfe6ff;font-weight:700")
        layout.addWidget(profile)
        return panel

    def scroll_page(self, page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def overview(self) -> QWidget:
        page, root = QWidget(), QVBoxLayout()
        welcome = QLabel("Центр управления НейроЗеткой")
        welcome.setStyleSheet("font-size:21px;font-weight:800;color:#8fe7ff;padding:8px 0")
        root.addWidget(welcome)
        self.dashboard_status = QLabel("Готова к запуску")
        self.dashboard_status.setStyleSheet("background:#18253a;border:1px solid #3c4b73;border-radius:10px;padding:14px;color:#dfe7ff;font-size:15px;font-weight:600")
        root.addWidget(self.dashboard_status)
        self.dashboard_mood = QLabel("Чат: Спокойное · НейроЗетка: Спокойная")
        self.dashboard_mood.setStyleSheet("background:#252b3d;border:1px solid #465174;border-radius:8px;padding:8px;color:#f5d77a;font-weight:600")
        root.addWidget(self.dashboard_mood)
        self.dashboard_mode = QLabel("Режим общения: дружелюбный")
        self.dashboard_mode.setStyleSheet("background:#152a31;border:1px solid #2e6573;border-radius:8px;padding:8px;color:#9fe9f7;font-weight:600")
        root.addWidget(self.dashboard_mode)
        actions = QHBoxLayout()
        start = QPushButton("Запустить / остановить бота")
        start.clicked.connect(self.toggle_bot)
        actions.addWidget(start)
        compact = QPushButton("Компактный режим поверх OBS")
        compact.clicked.connect(self.toggle_compact_mode)
        actions.addWidget(compact)
        obs = QPushButton("Открыть настройки OBS")
        obs.clicked.connect(lambda: self.tabs.setCurrentIndex(2))
        actions.addWidget(obs)
        mic = QPushButton("Открыть микрофон")
        mic.clicked.connect(lambda: self.tabs.setCurrentIndex(4))
        actions.addWidget(mic)
        root.addLayout(actions)
        emotion_actions = QHBoxLayout()
        for emotion in ("idle", "talking", "laugh", "angry"):
            button = QPushButton(f"Эмоция: {emotion}")
            button.clicked.connect(lambda _checked=False, state=emotion: self.show_character_in_obs(state))
            emotion_actions.addWidget(button)
        root.addLayout(emotion_actions)
        cards = QHBoxLayout()
        self.dashboard_cards = {}
        for name, hint in (("Twitch", "чат и зрители"), ("OBS", "кадр и персонаж"), ("LLM", "мозг и ответы"), ("STT", "голос")):
            card = QLabel(f"{name}\nНе проверен\n{hint}")
            card.setAlignment(Qt.AlignmentFlag.AlignCenter)
            card.setMinimumHeight(90)
            card.setStyleSheet("background:#151d2b;border:1px solid #30445f;border-radius:10px;padding:12px;color:#dfe6ff;font-weight:700")
            self.dashboard_cards[name] = card
            cards.addWidget(card)
        root.addLayout(cards)
        self.dashboard_stats = QLabel("Сообщений: 0  |  Ответов: 0  |  Зрителей: 0")
        self.dashboard_stats.setStyleSheet("color:#aeb5c9;padding:8px 0;font-weight:600")
        root.addWidget(self.dashboard_stats)
        self.active_viewers_label = QLabel("Активные зрители: пока нет данных")
        self.active_viewers_label.setWordWrap(True)
        self.active_viewers_label.setStyleSheet("background:#1b1f2c;border:1px solid #343b55;border-radius:8px;padding:10px;color:#dfe6ff")
        root.addWidget(self.active_viewers_label)
        hint = QLabel("Настройте Twitch, провайдера ИИ, OBS и микрофон в отдельных вкладках. Сохранение настроек выполняется кнопкой внизу окна.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8993aa;padding:12px 0")
        root.addWidget(hint)
        root.addStretch()
        page.setLayout(root)
        return page

    def toggle_compact_mode(self) -> None:
        compact = bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        if compact:
            self.setWindowFlags(Qt.WindowType.Window)
            self.setMinimumSize(720, 520)
            self.resize(1180, 760)
        else:
            self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
            self.setMinimumSize(420, 240)
            self.resize(560, 360)
        self.show(); self.raise_(); self.activateWindow()

    def connections(self) -> QWidget:
        page, layout = QWidget(), QVBoxLayout()
        twitch, tf = QGroupBox("Twitch IRC"), QFormLayout()
        tf.addRow("\u041a\u0430\u043d\u0430\u043b", self.line("twitch", "channel"))
        tf.addRow("Streamer Token", self.line("twitch", "streamer_token", True))
        tf.addRow("Bot Login", self.line("twitch", "bot_login"))
        tf.addRow("Bot OAuth Token", self.line("twitch", "bot_oauth_token", True))
        test = QPushButton("\u041e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0442\u0435\u0441\u0442 \u0432 Twitch")
        test.clicked.connect(self.test_twitch_message)
        tf.addRow(test)
        twitch.setLayout(tf)
        layout.addWidget(twitch); layout.addStretch(); page.setLayout(layout); return page

    def obs_connections(self) -> QWidget:
        page, root = QWidget(), QVBoxLayout()
        obs, form = QGroupBox("OBS WebSocket"), QFormLayout()
        form.addRow("Host", self.line("obs", "host"))
        form.addRow("Port", self.line("obs", "port"))
        form.addRow("Password", self.line("obs", "password", True))
        check = QPushButton("Подключить OBS и получить кадр")
        check.clicked.connect(self.check_obs)
        form.addRow(check)
        refresh = QPushButton("Получить сцены и источники из OBS")
        refresh.clicked.connect(self.refresh_obs_options)
        form.addRow(refresh)
        obs.setLayout(form)
        root.addWidget(obs)
        vision_box, vision_form = QGroupBox("Захват экрана и персонаж"), QFormLayout()
        self.preview = QLabel("OBS: кадр не получен")
        self.preview.setMinimumHeight(260)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background:#0d1018;border:1px solid #39425f;border-radius:8px;color:#8993aa")
        self.obs_capture_source = self.config_combo("vision_audio", "obs_source")
        self.obs_scene_combo = self.config_combo("obs", "scene")
        self.obs_image_source_combo = self.config_combo("obs", "image_source")
        vision_form.addRow("Источник OBS", self.obs_capture_source)
        vision_form.addRow("Интервал захвата (сек)", self.line("vision_audio", "capture_period_sec"))
        vision_form.addRow("Сцена персонажа", self.obs_scene_combo)
        vision_form.addRow("Image Source персонажа", self.obs_image_source_combo)
        image_row = QHBoxLayout()
        self.character_image = self.line("obs", "character_image")
        image_row.addWidget(self.character_image)
        choose_image = QPushButton("Выбрать изображение")
        choose_image.clicked.connect(self.choose_character_image)
        image_row.addWidget(choose_image)
        vision_form.addRow("Картинка персонажа", image_row)
        vision_form.addRow("Показывать персонажа (сек)", self.line("obs", "character_duration_sec"))
        vision_box.setLayout(vision_form)
        root.addWidget(vision_box)
        emotion_box, emotion_form = QGroupBox("Эмоции персонажа"), QFormLayout()
        self.emotion_images = {}
        for emotion, title in (("idle", "Покой"), ("talking", "Говорит"), ("laugh", "Смех"), ("angry", "Злость"), ("error", "Ошибка")):
            row = QHBoxLayout()
            field = self.line("obs", f"emotion_{emotion}")
            self.emotion_images[emotion] = field
            row.addWidget(field)
            choose = QPushButton("Выбрать фото")
            choose.clicked.connect(lambda _checked=False, state=emotion: self.choose_emotion_image(state))
            row.addWidget(choose)
            emotion_form.addRow(title, row)
        emotion_box.setLayout(emotion_form)
        root.addWidget(emotion_box)
        root.addWidget(self.preview)
        show_character = QPushButton("Показать персонажа в OBS сейчас")
        show_character.clicked.connect(self.show_character_in_obs)
        root.addWidget(show_character)
        hint = QLabel("В OBS включите WebSocket Server на том же порту. Сцена, Image Source и картинка персонажа настраиваются здесь.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#aeb5c9;padding:8px")
        root.addWidget(hint); root.addStretch(); page.setLayout(root); return page

    def ai(self) -> QWidget:
        page, root = QWidget(), QVBoxLayout(); box, form = QGroupBox("LLM Provider"), QFormLayout()
        self.provider = QComboBox(); self.provider.addItems(["Groq (\u0440\0435\043a\043e\043c\0435\043d\0434\0443\0435\0442\u0441\u044f)", "Gemini", "Auto"])
        self.provider.setCurrentIndex({"groq": 0, "gemini": 1, "auto": 2}.get(self.config.get("llm", "active_provider", "groq"), 0))
        self.provider.currentIndexChanged.connect(lambda i: self.config.set("llm", "active_provider", ("groq", "gemini", "auto")[i]))
        form.addRow("\u0410\043a\0442\0438\0432\u043d\044b\0439 \u043f\0440\043e\0432\0430\0439\0434\0435\0440", self.provider)
        form.addRow("Groq API Key", self.line("llm", "groq_api_key", True)); self.groq_model = self.combo("llm", "groq_model", GROQ_MODELS); form.addRow("Groq Model", self.groq_model)
        form.addRow("Gemini API Key", self.line("llm", "gemini_api_key", True)); self.gemini_model = self.combo("llm", "gemini_model", GEMINI_MODELS); form.addRow("Gemini Model", self.gemini_model)
        check = QPushButton("\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u043a\u043b\u044e\u0447 \u0438 \u043c\u043e\u0434\u0435\u043b\u044c"); check.clicked.connect(self.validate_llm); form.addRow(check)
        response_test = QPushButton("\u0422\u0435\u0441\u0442 \u043d\u0435\u0439\u0440\u043e\u043d\u043a\u0438 \u2192 Twitch"); response_test.clicked.connect(self.test_llm_to_twitch); form.addRow(response_test)
        box.setLayout(form); root.addWidget(box); root.addWidget(QLabel("\u0421\u0438\u0441\u0442\u0435\u043c\u043d\u044b\u0439 \u043f\u0440\u043e\u043c\u043f\u0442"))
        self.prompt = QPlainTextEdit(str(self.config.get("llm", "system_prompt", ""))); self.prompt.setFont(QFont("Consolas", 11)); self.prompt.textChanged.connect(lambda: self.config.set("llm", "system_prompt", self.prompt.toPlainText())); root.addWidget(self.prompt); page.setLayout(root); return page

    def combo(self, section: str, key: str, values: list[str]) -> QComboBox:
        item = QComboBox(); item.addItems(values); item.setCurrentText(str(self.config.get(section, key, values[0]))); item.currentTextChanged.connect(lambda value: self.config.set(section, key, value.strip())); return item

    def config_combo(self, section: str, key: str) -> QComboBox:
        item = QComboBox()
        item.setEditable(True)
        item.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        item.setCurrentText(str(self.config.get(section, key, "") or ""))
        item.currentTextChanged.connect(lambda value: self.config.set(section, key, value.strip()))
        return item

    def refresh_obs_options(self) -> None:
        task = NetworkTask(self.fetch_obs_options)
        self.model_tasks.append(task)
        task.finished.connect(self.apply_obs_options)
        task.finished.connect(lambda *_: self.model_tasks.remove(task) if task in self.model_tasks else None)
        task.start()

    def fetch_obs_options(self) -> str:
        import obsws_python as obs
        client = obs.ReqClient(
            host=self.config.get("obs", "host", "127.0.0.1"),
            port=int(self.config.get("obs", "port", 4455)),
            password=self.config.get("obs", "password"),
            timeout=5,
        )
        scene_data = client.get_scene_list()
        input_data = client.get_input_list()
        scenes = [str(getattr(item, "scene_name", "")).strip() for item in getattr(scene_data, "scenes", [])]
        sources = [str(getattr(item, "input_name", "")).strip() for item in getattr(input_data, "inputs", [])]
        current_scene_response = client.get_current_program_scene()
        current_scene = str(getattr(current_scene_response, "current_program_scene_name", "")).strip()
        image_sources = [str(getattr(item, "input_name", "")).strip() for item in getattr(input_data, "inputs", []) if str(getattr(item, "input_kind", "")).lower() in {"image_source", "browser_source"}]
        return repr((sorted(item for item in scenes if item), sorted(item for item in sources if item), current_scene, sorted(set(image_sources))))

    def apply_obs_options(self, ok: bool, text: str) -> None:
        if not ok:
            self.events.status.emit("OBS", False, f"Не удалось получить список OBS: {text}")
            return
        try:
            parsed = ast.literal_eval(text)
            scenes, sources = parsed[0], parsed[1]
            current_scene = parsed[2] if len(parsed) > 2 else ""
            image_sources = parsed[3] if len(parsed) > 3 else []
        except (ValueError, SyntaxError) as exc:
            self.events.status.emit("OBS", False, f"Некорректный ответ OBS: {exc}")
            return
        for combo, values, key in (
            (self.obs_scene_combo, scenes, "scene"),
            (self.obs_image_source_combo, image_sources or sources, "image_source"),
            (self.obs_capture_source, sources, "obs_source"),
        ):
            current = str(self.config.get("obs", key, "") if key != "obs_source" else self.config.get("vision_audio", key, "") or "")
            combo.blockSignals(True)
            combo.clear(); combo.addItems(values)
            combo.setCurrentText(current if current in values else (values[0] if values else current))
            combo.blockSignals(False)
        if current_scene and self.config.get("obs", "auto_scene", True):
            self.obs_scene_combo.setCurrentText(current_scene)
        self.events.status.emit("OBS", True, f"Загружено сцен: {len(scenes)}, источников: {len(sources)}")

    def vision(self) -> QWidget:
        page, layout = QWidget(), QVBoxLayout()
        self.wake_info = QLabel("Бот отвечает только на обращения: НейроЗетка, Зеточка, Зетка, neyrozetka")
        self.wake_info.setWordWrap(True); layout.addWidget(self.wake_info)
        self.mic_combo = QComboBox()
        self.refresh_microphones()
        layout.addWidget(QLabel("Микрофон для распознавания"))
        layout.addWidget(self.mic_combo)
        layout.addWidget(QLabel("Чувствительность микрофона"))
        self.mic_sensitivity = QComboBox()
        self.mic_sensitivity.addItem("Высокая — тихая речь", 1.35)
        self.mic_sensitivity.addItem("Обычная", 1.8)
        self.mic_sensitivity.addItem("Низкая — меньше фонового шума", 2.4)
        current_sensitivity = float(self.config.get("vision_audio", "microphone_sensitivity", 1.8) or 1.8)
        closest = min(range(self.mic_sensitivity.count()), key=lambda index: abs(float(self.mic_sensitivity.itemData(index)) - current_sensitivity))
        self.mic_sensitivity.setCurrentIndex(closest)
        self.mic_sensitivity.currentIndexChanged.connect(
            lambda index: self.save_microphone_setting("microphone_sensitivity", self.mic_sensitivity.itemData(index))
        )
        layout.addWidget(self.mic_sensitivity)
        self.mic_level = QProgressBar()
        self.mic_level.setRange(0, 100)
        self.mic_level.setValue(0)
        self.mic_level.setFormat("Уровень сигнала: %p%")
        layout.addWidget(self.mic_level)
        speech_box = QGroupBox("Распознанная речь")
        speech_layout = QVBoxLayout()
        self.last_speech = QLabel("Последняя фраза: пока нет данных")
        self.last_speech.setWordWrap(True)
        self.last_speech.setStyleSheet(
            "background:#171d2b;border:1px solid #8061c7;border-radius:8px;"
            "padding:12px;color:#f5d77a;font-size:14px;font-weight:600"
        )
        speech_layout.addWidget(self.last_speech)
        speech_layout.addWidget(QLabel("История распознавания (для проверки точности)"))
        self.speech_history = QPlainTextEdit()
        self.speech_history.setReadOnly(True)
        self.speech_history.setMaximumHeight(150)
        speech_layout.addWidget(self.speech_history)
        speech_box.setLayout(speech_layout)
        layout.addWidget(speech_box)
        controls = QHBoxLayout()
        mic = QPushButton("Проверить микрофон"); mic.clicked.connect(self.check_microphone); controls.addWidget(mic)
        calibrate = QPushButton("Калибровать шум"); calibrate.clicked.connect(self.calibrate_microphone); controls.addWidget(calibrate)
        listen = QPushButton("Включить микрофон"); listen.clicked.connect(self.toggle_voice); self.voice_button = listen; controls.addWidget(listen)
        obs_connect = QPushButton("Подключить OBS и получить кадр"); obs_connect.clicked.connect(self.check_obs); controls.addWidget(obs_connect)
        layout.addLayout(controls); page.setLayout(layout); return page

    def save_microphone_setting(self, key: str, value: Any) -> None:
        self.config.set("vision_audio", key, value)
        try:
            self.config.save()
        except OSError:
            LOGGER.exception("Could not save microphone setting")

    def calibrate_microphone(self) -> None:
        device = self.mic_combo.currentData()
        if device == -1:
            self.events.status.emit("STT", False, "Сначала выберите доступный микрофон")
            return
        self.events.status.emit("STT", True, "Калибрую шум микрофона 2 секунды...")

        def work() -> None:
            try:
                import numpy as np
                import sounddevice as sd
                samples: list[float] = []
                with sd.InputStream(samplerate=VOICE_SAMPLE_RATE, blocksize=int(VOICE_SAMPLE_RATE * 0.25), channels=1, dtype="int16", device=int(device)) as stream:
                    for _ in range(8):
                        frame, _ = stream.read(int(VOICE_SAMPLE_RATE * 0.25))
                        values = frame[:, 0].astype(np.float32)
                        samples.append(float(np.sqrt(np.mean(np.square(values)) + 1.0)))
                floor = max(30.0, float(np.median(samples)))
                self.save_microphone_setting("microphone_noise_floor", floor)
                self.events.status.emit("STT", True, f"Шум откалиброван: {floor:.0f}")
            except Exception as exc:
                LOGGER.exception("Microphone calibration failed")
                self.events.status.emit("STT", False, f"Калибровка не удалась: {exc}")

        threading.Thread(target=work, daemon=True, name="Microphone-calibration").start()

    def choose_character_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Выберите картинку НейроЗетки", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if not path:
            return
        target_dir = DATA_DIR / "character"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / Path(path).name
        if Path(path).resolve() != target.resolve():
            shutil.copy2(path, target)
        self.character_image.setText(str(target))
        self.setWindowIcon(QIcon(str(target)))
        self.statusBar().showMessage("Картинка персонажа сохранена", 5000)

    def choose_emotion_image(self, emotion: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Выберите изображение: {emotion}",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.gif)",
        )
        if not path:
            return
        target_dir = DATA_DIR / "character" / "emotions"
        target_dir.mkdir(parents=True, exist_ok=True)
        source = Path(path)
        target = target_dir / f"{emotion}_{source.name}"
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        field = self.emotion_images[emotion]
        field.setText(str(target))
        self.statusBar().showMessage(f"Эмоция «{emotion}» сохранена", 5000)

    def chat(self) -> QWidget:
        page, root = QWidget(), QVBoxLayout(); split = QSplitter(Qt.Orientation.Horizontal); self.chat_table = QTableWidget(0, 3); self.chat_table.setHorizontalHeaderLabels(["\u0412\u0440\u0435\u043c\u044f", "\u0410\u0432\u0442\u043e\u0440", "\u0421\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435"]); self.chat_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch); self.chat_table.setAlternatingRowColors(True); self.chat_table.setSortingEnabled(True); split.addWidget(self.chat_table); self.log_box = QPlainTextEdit(); self.log_box.setReadOnly(True); self.log_box.setPlaceholderText("События подключения, OBS и ответы модели появятся здесь..."); split.addWidget(self.log_box); root.addWidget(split); page.setLayout(root); return page

    def memory(self) -> QWidget:
        page, root = QWidget(), QVBoxLayout(); self.memory_table = QTableWidget(0, 4); self.memory_table.setHorizontalHeaderLabels(["ID", "\u0410\u0432\u0442\u043e\u0440", "\u0422\u0435\u043a\u0441\u0442", "\u0414\u0430\u0442\u0430"]); self.memory_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch); root.addWidget(self.memory_table); button = QPushButton("\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u043e\u0435 \u0432\u043e\u0441\u043f\u043e\u043c\u0438\u043d\u0430\u043d\u0438\u0435"); button.clicked.connect(self.delete_memory); root.addWidget(button); self.load_memory(); page.setLayout(root); return page

    def bind(self) -> None:
        self.events.status.connect(self.status); self.events.chat.connect(self.chat_message); self.events.display.connect(self.display_message); self.events.speech.connect(self.display_speech); self.events.log.connect(lambda text, _: self.log_box.appendPlainText(text))

    def update_provider_pills(self, index: int | None = None) -> None:
        active = ("Groq", "Gemini", "Auto")[self.provider.currentIndex()]
        for name, pill in self.provider_pills.items():
            if name == active:
                pill.setStyleSheet(
                    "background:#3d2c63;border:1px solid #a980ff;border-radius:8px;"
                    "padding:6px 10px;color:#f2e9ff;font-weight:700;min-width:90px"
                )
            else:
                pill.setStyleSheet(
                    "background:#20283d;border:1px solid #445074;border-radius:8px;"
                    "padding:6px 10px;color:#dfe6ff;font-weight:600;min-width:90px"
                )

    def display_message(self, author: str, text: str) -> None:
        self.append_chat_row(author, text)

    def append_chat_row(self, author: str, text: str) -> None:
        sorting = self.chat_table.isSortingEnabled()
        self.chat_table.setSortingEnabled(False)
        row = self.chat_table.rowCount()
        self.chat_table.insertRow(row)
        for col, value in enumerate((time.strftime("%H:%M:%S"), author, text)):
            self.chat_table.setItem(row, col, QTableWidgetItem(value))
        while self.chat_table.rowCount() > 500:
            self.chat_table.removeRow(0)
        self.chat_table.setSortingEnabled(sorting)
        self.chat_table.scrollToBottom()

    def display_speech(self, text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.last_speech.setText(f"Последняя фраза ({timestamp}): {text}")
        self.speech_history.appendPlainText(f"[{timestamp}] {text}")
        while self.speech_history.document().blockCount() > 100:
            cursor = self.speech_history.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()

    def session_context(self, author: str) -> str:
        now = time.time()
        stream_started = self.stream_started_at or now
        first_seen = self.viewer_seen_at.get(author)
        viewer_line = f"Зритель {author} впервые замечен в чате: {time.strftime('%H:%M:%S', time.localtime(first_seen))}." if first_seen else f"Зритель {author} ещё не отмечен в текущей сессии."
        stream_lines = "\n".join(f"{viewer}: {message}" for viewer, message in self.stream_memory[-12:])
        return (
            f"Текущая сессия стрима началась примерно {time.strftime('%H:%M:%S', time.localtime(stream_started))}; "
            f"она длится {int(max(0, now - stream_started) // 60)} минут. {viewer_line} "
            f"\nПоследние сообщения текущего стрима:\n{stream_lines}\n"
            f"Эмоциональный фон чата: {self.chat_mood}, сила {self.emotion_intensity:.2f}. "
            f"Собственное состояние НейроЗетки: {self.bot_mood}, сила {self.bot_emotion_intensity:.2f}.\n"
            "Используй это только когда вопрос относится к стриму, зрителю или времени."
        )

    def status(self, name: str, ok: bool, text: str) -> None:
        key = {"Twitch IRC": "Twitch", "OBS": "OBS", "LLM": "LLM"}.get(name, name)
        if hasattr(self, "dashboard_status"):
            self.dashboard_status.setText(f"{key}: {text}")
        if key in getattr(self, "dashboard_cards", {}):
            self.dashboard_cards[key].setText(f"{key}\n{'Подключено' if ok else 'Ошибка'}\n{text}")
        if key in self.badges:
            self.badges[key].setText(f"{key}: ● {text}")
            self.badges[key].setStyleSheet(
                "background:#193326;border:1px solid #3ac77d;border-radius:8px;"
                "padding:6px 10px;color:#72e6a5;font-weight:700"
                if ok else
                "background:#3a261d;border:1px solid #ff7f5a;border-radius:8px;"
                "padding:6px 10px;color:#ffb199;font-weight:700"
            )
        if name == "Twitch IRC" and ok:
            self.live_indicator.setText("● ОНЛАЙН")
            self.live_indicator.setStyleSheet("color:#72e6a5;font-weight:700;padding:8px 12px;background:#193326;border-radius:8px")
        elif name == "Twitch IRC" and not ok:
            self.live_indicator.setText("● НУЖНА ПРОВЕРКА")
            self.live_indicator.setStyleSheet("color:#ffb86b;font-weight:700;padding:8px 12px;background:#3a291d;border-radius:8px")
        self.status_panel.setText(f"Состояние: {text}")
        self.status_panel.setStyleSheet(
            "background:#18253a;border:1px solid #3c4b73;border-radius:10px;"
            "padding:8px 12px;color:#dfe7ff;font-weight:600"
            if ok else
            "background:#3a261d;border:1px solid #ff7f5a;border-radius:10px;"
            "padding:8px 12px;color:#ffd9ce;font-weight:700"
        )
        self.log_box.appendPlainText(text)
        while self.log_box.document().blockCount() > 500:
            cursor = self.log_box.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()
        if key == "OBS":
            self.preview.setToolTip(text)

    def chat_message(self, author: str, text: str) -> None:
        self.append_chat_row(author, text)
        threading.Thread(target=self.memory_store.add, args=(author, text), daemon=True, name="Memory-write").start()
        self.stream_memory.append((author, text))
        self.stream_memory = self.stream_memory[-40:]
        self.active_viewers.add(author)
        self.stream_stats["messages"] += 1
        self.update_stream_mood(text)
        self.update_dashboard_stats()
        self.viewer_seen_at.setdefault(author, time.time())
        if self.stream_started_at is None:
            self.stream_started_at = time.time()
        LOGGER.info("Twitch chat message author=%s length=%d", author, len(text))
        lowered = text.casefold().strip()
        if lowered.startswith("!emotion "):
            emotion = lowered.split(None, 1)[1].strip()
            if emotion in {"idle", "talking", "laugh", "angry", "error"}:
                self.chat_emotion = emotion
                self.show_character_in_obs(emotion)
                self.events.log.emit(f"Эмоция OBS переключена: {emotion}", "success")
            return
        if lowered == "!mood":
            if self.twitch:
                self.twitch.send(f"Настроение чата: {self.chat_mood}")
            return
        if lowered == "!stats":
            if self.twitch:
                self.twitch.send(f"Сообщений: {self.stream_stats['messages']}, ответов: {self.stream_stats['answers']}, зрителей: {len(self.active_viewers)}")
            return
        if lowered.startswith(("запомни:", "запомни ")):
            fact = text.split(":", 1)[1].strip() if ":" in text else text[7:].strip()
            if fact:
                self.memory_store.remember_fact(author, fact)
                self.events.log.emit(f"Сохранила постоянный факт о {author}", "success")
                self.events.display.emit("Память", f"Запомнила: {fact}")
            return
        wake_words = tuple(str(word).strip().lower() for word in self.config.get("vision_audio", "wake_words", list(WAKE_WORDS)) if str(word).strip())
        addressed = is_addressed(text)
        if self.bot_running and author.lower() != str(self.config.get("twitch", "bot_login", "")).lower() and addressed:
            now = time.time()
            previous = self.last_answer_by_author.get(author.casefold())
            if previous and now - previous[0] < self.answer_cooldown_seconds:
                self.events.log.emit(f"Антиспам: пауза для {author}", "info")
                return
            if self.last_answer_text and text.casefold() == self.last_answer_text.casefold() and now - self.last_answer_at < 20:
                self.events.log.emit("Антиспам: повторный запрос пропущен", "info")
                return
            self.last_answer_by_author[author.casefold()] = (now, text)
            self.last_answer_text, self.last_answer_at = text, now
            self.events.log.emit(f"Обращение к НейроЗетке от {author}: ответ готовится", "info")
            threading.Thread(target=self.answer_chat, args=(author, text), daemon=True).start()

    def viewer_joined(self, author: str) -> None:
        if author and author.lower() != str(self.config.get("twitch", "bot_login", "")).lower():
            self.viewer_seen_at.setdefault(author, time.time())
            self.active_viewers.add(author)
            self.update_dashboard_stats()
            self.events.log.emit(f"Зритель {author} вошёл в чат", "info")

    def update_stream_mood(self, text: str) -> None:
        emotion, mood, intensity = self.detect_emotion(text)
        self.emotion_intensity = intensity
        self.chat_mood = mood
        self.chat_emotion = "laugh" if emotion == "laugh" else "angry" if emotion == "angry" else "idle"
        if self.chat_emotion in {"laugh", "angry"}:
            self.show_character_in_obs(self.chat_emotion)
        if not intensity:
            self.chat_mood, self.chat_emotion = "Спокойное", "idle"
        self.update_mood_label()

    def detect_emotion(self, text: str) -> tuple[str, str, float]:
        lowered = text.casefold()
        emotion_words = {
            "laugh": ("ахах", "хаха", "лол", "кек", "смешно", "😂"),
            "angry": ("бесит", "злюсь", "раздражает", "ненавижу", "отстань", "идиот", "ужас", "😡"),
            "surprised": ("вау", "ого", "невероятно", "шок", "серьёзно", "😮", "ничего себе"),
            "sad": ("грустно", "печально", "жаль", "плачу", "😭"),
            "happy": ("рад", "рада", "ура", "класс", "люблю", "прекрасно", "💚", "❤️"),
        }
        scores = {emotion: sum(lowered.count(token) for token in words) for emotion, words in emotion_words.items()}
        emotion, score = max(scores.items(), key=lambda item: item[1])
        intensity = min(1.0, 0.35 + score * 0.2 + min(0.3, lowered.count("!") * 0.05)) if score else 0.0
        labels = {"laugh": "Весёлое", "angry": "Злое", "surprised": "Удивлённое", "sad": "Грустное", "happy": "Радостное"}
        return emotion, labels.get(emotion, "Спокойное"), intensity

    def update_bot_emotion(self, text: str) -> None:
        emotion, mood, intensity = self.detect_emotion(text)
        self.bot_mood, self.bot_emotion_intensity = mood, intensity
        self.bot_emotion = "laugh" if emotion in {"laugh", "happy"} else "angry" if emotion == "angry" else "idle"
        self.update_mood_label()

    def update_mood_label(self) -> None:
        if hasattr(self, "dashboard_mood"):
            self.dashboard_mood.setText(f"Чат: {self.chat_mood} · НейроЗетка: {self.bot_mood} · сила чата {int(self.emotion_intensity * 100)}%")
        if hasattr(self, "dashboard_mode"):
            if self.chat_mood in {"Злое", "Грустное"}:
                mode = "спокойный и поддерживающий"
            elif self.chat_mood in {"Весёлое", "Радостное"}:
                mode = "игривый, можно мягко пошутить"
            else:
                mode = "дружелюбный и естественный"
            self.dashboard_mode.setText(f"Режим общения: {mode}")

    def update_dashboard_stats(self) -> None:
        if hasattr(self, "dashboard_stats"):
            self.dashboard_stats.setText(
                f"Сообщений: {self.stream_stats['messages']}  |  Ответов: {self.stream_stats['answers']}  |  Зрителей: {len(self.active_viewers)}"
            )
        if hasattr(self, "active_viewers_label"):
            viewers = ", ".join(sorted(self.active_viewers)[-20:]) or "пока нет данных"
            self.active_viewers_label.setText(f"Активные зрители: {viewers}")

    def answer_chat(self, author: str, text: str) -> None:
        try:
            self.show_character_in_obs("talking")
            answer = LLMService(self.config, self.memory_store, self.events).complete(text, author, self.session_context(author))
            self.update_bot_emotion(answer)
            self.stream_stats["answers"] += 1
            self.last_answer_by_author[author.casefold()] = (time.time(), answer)
            self.update_dashboard_stats()
            if self.twitch:
                self.twitch.send(answer)
                self.events.log.emit("Ответ НейроЗетки поставлен в очередь Twitch", "success")
            self.events.display.emit("НейроЗетка", answer)
            laugh_markers = ("ахах", "хаха", "лол", "смеш", "😂")
            response_emotion = "laugh" if any(marker in answer.casefold() for marker in laugh_markers) else "angry" if self.bot_emotion == "angry" or self.chat_emotion == "angry" else "talking"
            self.show_character_in_obs(response_emotion)
        except Exception:
            LOGGER.exception("Chat answer worker crashed")
            self.show_character_in_obs("error")
            self.events.log.emit(f"Критическая ошибка ответа. Лог: {LOG_PATH}", "error")

    def show_character_in_obs(self, emotion: str = "idle") -> None:
        scene = str(self.config.get("obs", "scene", "") or "").strip()
        source = str(self.config.get("obs", "image_source", "NeyroZetka") or "").strip()
        image = str(self.config.get("obs", f"emotion_{emotion}", "") or "").strip()
        if not image or not Path(image).exists():
            image = str(self.config.get("obs", "character_image", "") or "").strip()
        if not scene or not source or not image or not Path(image).exists():
            self.events.status.emit("OBS", False, "Заполните сцену, Image Source и картинку персонажа")
            return
        duration = max(1, int(self.config.get("obs", "character_duration_sec", 5) or 5))

        def work() -> None:
            try:
                import obsws_python as obs
                client = obs.ReqClient(
                    host=self.config.get("obs", "host", "127.0.0.1"),
                    port=int(self.config.get("obs", "port", 4455)),
                    password=self.config.get("obs", "password"),
                    timeout=5,
                )
                client.set_input_settings(source, {"file": image}, True)
                try:
                    item = client.get_scene_item_id(scene, source)
                except Exception:
                    client.create_input(scene, source, "image_source", {"file": image, "unload": False}, True)
                    item = client.get_scene_item_id(scene, source)
                item_id = getattr(item, "scene_item_id", None)
                if item_id is None:
                    raise RuntimeError(f"Источник «{source}» не найден в сцене «{scene}»")
                client.set_scene_item_enabled(scene, item_id, True)
                self.events.status.emit("OBS", True, f"Персонаж показан в OBS на {duration} сек.")
                time.sleep(duration)
                client.set_scene_item_enabled(scene, item_id, False)
            except Exception as exc:
                LOGGER.exception("OBS character overlay failed")
                self.events.status.emit("OBS", False, f"Персонаж OBS: {exc}")

        threading.Thread(target=work, daemon=True, name="OBS-character").start()

    def test_twitch_message(self) -> None:
        if not self.twitch or not self.bot_running:
            self.events.status.emit("Twitch IRC", False, "Сначала запустите бота и дождитесь авторизации Twitch")
            return
        self.twitch.send("Тест НейроЗетки: отправка сообщений в Twitch работает.")
        self.events.log.emit("Тестовое сообщение поставлено в очередь Twitch", "success")

    def test_llm_to_twitch(self) -> None:
        if not self.twitch or not self.bot_running:
            self.events.status.emit("Twitch IRC", False, "????????? ???? ? ????????? ??????????? Twitch")
            return
        self.events.log.emit("LLM test: generating a Twitch chat response", "info")
        def work() -> None:
            answer = LLMService(self.config, self.memory_store, self.events).complete(
                "???????? ?????. ?????? ???????: ???????? ?? ?????.", "????????"
            )
            self.twitch.send("???????? ??????????: " + answer)
            self.events.log.emit("LLM test response queued for Twitch", "success")
        threading.Thread(target=work, daemon=True, name="LLM-Twitch-test").start()

    def check_microphone(self) -> None:
        try:
            import sounddevice as sd
            inputs = [device for device in sd.query_devices()
                      if int(device.get("max_input_channels", 0)) > 0]
            if not inputs:
                raise RuntimeError("input device not found")
            self.events.status.emit("STT", True, "Микрофон найден: " + str(self.mic_combo.currentText()))
            self.events.log.emit("Микрофон выбран. Нажмите «Начать слушать» для распознавания речи.", "info")
        except ImportError:
            self.events.status.emit("STT", False, "Модуль sounddevice не установлен")
        except Exception as exc:
            LOGGER.exception("Microphone check failed")
            self.events.status.emit("STT", False, f"Микрофон недоступен: {exc}")

    def refresh_microphones(self) -> None:
        self.mic_combo.clear()
        try:
            import sounddevice as sd
            preferred = str(self.config.get("vision_audio", "microphone_name", "Микрофон (fifine Microphone)") or "").casefold()
            preferred_index = -1
            for index, device in enumerate(sd.query_devices()):
                if int(device.get("max_input_channels", 0)) > 0:
                    name = str(device.get("name", "input"))
                    self.mic_combo.addItem(f"{index}: {name}", index)
                    if preferred and preferred in name.casefold():
                        preferred_index = self.mic_combo.count() - 1
            if not self.mic_combo.count():
                self.mic_combo.addItem("Микрофон не найден", -1)
            elif preferred_index >= 0:
                self.mic_combo.setCurrentIndex(preferred_index)
            self.mic_combo.currentTextChanged.connect(
                lambda value: self.save_microphone_setting("microphone_name", value.split(": ", 1)[-1].strip())
            )
        except Exception as exc:
            LOGGER.exception("Microphone enumeration failed")
            self.mic_combo.addItem(f"Ошибка списка микрофонов: {exc}", -1)

    def refresh_groq_models(self, *, silent: bool = False) -> None:
        api_key = str(self.config.get("llm", "groq_api_key", "") or "").strip()
        if not api_key:
            self.groq_model.clear()
            self.groq_model.addItems(GROQ_MODELS)
            self.config.set("llm", "groq_model", GROQ_MODELS[0])
            return
        task = NetworkTask(lambda: list_groq_text_models(api_key))
        self.model_tasks.append(task)
        task.finished.connect(lambda ok, text: self._apply_groq_models(ok, text, name="Groq", combo=self.groq_model, key="groq_model", silent=silent))
        task.finished.connect(lambda *_: self.model_tasks.remove(task) if task in self.model_tasks else None)
        task.start()

    def refresh_gemini_models(self, *, silent: bool = False) -> None:
        api_key = str(self.config.get("llm", "gemini_api_key", "") or "").strip()
        if not api_key:
            self.gemini_model.clear()
            self.gemini_model.addItems(GEMINI_MODELS)
            self.config.set("llm", "gemini_model", GEMINI_MODELS[0])
            return
        task = NetworkTask(lambda: list_gemini_text_models(api_key))
        self.model_tasks.append(task)
        task.finished.connect(lambda ok, text: self._apply_groq_models(ok, text, name="Gemini", combo=self.gemini_model, key="gemini_model", silent=silent))
        task.finished.connect(lambda *_: self.model_tasks.remove(task) if task in self.model_tasks else None)
        task.start()

    def _apply_groq_models(self, ok: bool, text: str, *, name: str, combo: QComboBox, key: str, silent: bool = False) -> None:
        if not ok:
            if not silent:
                self.events.status.emit("LLM", False, text)
            return
        try:
            models = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            models = [item.strip() for item in str(text).split(",") if item.strip()]
        if not models:
            return
        current = str(self.config.get("llm", key, models[0]))
        combo.blockSignals(True)
        combo.clear(); combo.addItems(models)
        if current in models:
            combo.setCurrentText(current)
        else:
            combo.setCurrentIndex(0)
            self.config.set("llm", key, models[0])
        combo.blockSignals(False)
        if not silent:
            self.events.status.emit("LLM", True, f"Загружено {len(models)} текстовых моделей {name}")

    def toggle_voice(self) -> None:
        if self.voice_thread and self.voice_thread.isRunning():
            self.voice_thread.stop()
            self.voice_thread.wait(2000)
            self.voice_button.setText("Включить микрофон")
            self.events.status.emit("STT", False, "Слушание остановлено")
            self.voice_thread = None
            return
        if self.mic_combo.currentData() == -1:
            self.check_microphone()
            return
        api_key = self.config.get("llm", "groq_api_key")
        if not api_key:
            self.events.status.emit("STT", False, "Для распознавания нужен Groq API key")
            return
        self.voice_button.setText("Остановить слушание")
        self.events.status.emit("STT", True, "Слушаю микрофон")
        device = int(self.mic_combo.currentData())
        self.voice_thread = VoiceWorker(self.config, device, self.events)
        self.voice_thread.state.connect(lambda name, ok, text: self.events.status.emit(name, ok, text))
        self.voice_thread.error.connect(lambda text: self.events.status.emit("STT", False, text))
        self.voice_thread.level.connect(lambda value: self.mic_level.setValue(int(value * 100)))
        self.voice_thread.transcript.connect(self.handle_voice_transcript)
        self.voice_thread.start()

    def handle_voice_transcript(self, text: str) -> None:
        self.events.log.emit(f"Вы сказали: {text}", "info")
        self.events.display.emit("Вы (голос)", text)
        self.events.speech.emit(text)
        if is_addressed(text) and self.bot_running:
            self.events.log.emit("Обращение обнаружено — запрашиваю ответ нейронки", "info")
            if self.voice_answer_lock.acquire(blocking=False):
                threading.Thread(target=self.voice_answer_worker, args=(text,), daemon=True, name="Voice-answer").start()
            else:
                self.events.log.emit("Предыдущий голосовой ответ ещё готовится", "info")
        else:
            self.events.log.emit("Обращения к боту нет или бот остановлен — ответ не отправляется", "info")

    def voice_answer_worker(self, text: str) -> None:
        try:
            self.answer_chat("Стример", "[Стример голосом]: " + text)
        finally:
            self.voice_answer_lock.release()

    def test_vision_to_twitch(self) -> None:
        if not self.twitch or not self.bot_running:
            self.events.status.emit("Twitch IRC", False, "????????? ???? ? ????????? ??????????? Twitch")
            return
        if not self.obs or not self.obs.isRunning():
            self.events.status.emit("OBS", False, "??????? ?????????? OBS WebSocket")
            return
        if not self.last_frame_at:
            self.events.status.emit("OBS", False, "OBS ?????????, ?? ???? ??? ?? ???????")
            return
        self.twitch.send("???????? ??????: ???? OBS ???????, ?????????? ????? ?????.")
        self.events.log.emit("OBS frame test response queued for Twitch", "success")

    def toggle_bot(self) -> None:
        if self.bot_running:
            self.stop_bot(); return
        self.config.save()
        missing = []
        if not self.config.get("twitch", "bot_oauth_token"): missing.append("Twitch Bot OAuth Token")
        if not self.config.get("twitch", "bot_login"): missing.append("Twitch Bot Login")
        if not self.config.get("twitch", "channel"): missing.append("Twitch channel")
        if missing:
            message = "Нельзя запустить бота. Заполните: " + ", ".join(missing)
            self.events.status.emit("Twitch IRC", False, message)
            self.statusBar().showMessage(message, 8000)
            return
        provider = ("groq", "gemini", "auto")[self.provider.currentIndex()]
        provider_order = ["groq", "gemini"] if provider == "auto" else [provider]
        selected = None
        for candidate in provider_order:
            key = self.config.get("llm", f"{candidate}_api_key")
            if not key:
                continue
            try:
                models = list_groq_text_models(key) if candidate == "groq" else list_gemini_text_models(key)
                current_key = "groq_model" if candidate == "groq" else "gemini_model"
                current = self.config.get("llm", current_key, models[0])
                if current not in models:
                    self.config.set("llm", current_key, models[0])
                    getattr(self, f"{candidate}_model").setCurrentText(models[0])
                self.events.status.emit("LLM", True, f"{candidate.title()} готов: {self.config.get('llm', current_key)}")
                selected = candidate
                break
            except Exception as exc:
                self.events.status.emit("LLM", False, f"{candidate.title()} недоступен: {exc}")
                if provider != "auto":
                    self.statusBar().showMessage(f"{candidate.title()}: {exc}", 8000)
                    return
        if selected is None:
            self.events.status.emit("LLM", False, "Не найден доступный провайдер с валидным API key")
            self.statusBar().showMessage("Нет доступного провайдера. Проверьте API keys.", 8000)
            return
        self.bot_running = True
        self.stream_started_at = time.time()
        self.stream_stats = {"messages": 0, "answers": 0, "started": self.stream_started_at}
        self.active_viewers.clear()
        self.chat_mood = "Спокойное"
        self.bot_mood = "Спокойное"
        self.update_dashboard_stats()
        self.chat_emotion = "idle"
        self.emotion_intensity = 0.0
        self.bot_emotion = "idle"
        self.bot_emotion_intensity = 0.0
        self.update_mood_label()
        self.twitch = TwitchWorker(self.config)
        self.twitch.message.connect(self.chat_message)
        self.twitch.viewer_joined.connect(self.viewer_joined)
        self.twitch.state.connect(lambda ok, text: self.events.status.emit("Twitch IRC", ok, text))
        self.twitch.start()
        if self.mic_combo.currentData() != -1 and self.config.get("llm", "groq_api_key"):
            self.toggle_voice()
        self.start_button.setText("ОСТАНОВТЬ БОТА")
        self.start_button.setStyleSheet("background:#d73a49;font-weight:bold")

    def stop_bot(self) -> None:
        self.bot_running = False
        if self.twitch: self.twitch.stop(); self.twitch.wait(2000)
        self.start_button.setText("ЗАПУСТТЬ БОТА"); self.start_button.setStyleSheet("background:#2ea44f;font-weight:bold")

    def save_config(self) -> None:
        try:
            if self.config.path.exists():
                shutil.copy2(self.config.path, self.config.path.with_suffix(".backup.json"))
            self.config.save()
            LOGGER.info("Configuration saved to %s", CONFIG_PATH)
            self.statusBar().showMessage("Настройки успешно сохранены!", 5000)
        except Exception:
            LOGGER.exception("Configuration save failed")
            QMessageBox.critical(self, "Ошибка сохранения", f"Не удалось сохранить config.json.\nПодробности: {LOG_PATH}")

    def open_log(self) -> None:
        try:
            if not LOG_PATH.exists():
                LOG_PATH.touch()
            os.startfile(LOG_PATH)
        except OSError as exc:
            LOGGER.exception("Cannot open crash log")
            QMessageBox.warning(self, "Лог недоступен", f"{LOG_PATH}\n{exc}")

    def check_obs(self) -> None:
        if self.obs and self.obs.isRunning():
            self.obs.stop(); self.obs.wait(2000)
        self.obs = OBSWorker(self.config); self.obs.state.connect(lambda ok, text: self.events.status.emit("OBS", ok, text)); self.obs.frame.connect(self.show_frame); self.obs.start()
        self.refresh_obs_options()

    def validate_llm(self) -> None:
        provider = ("groq", "gemini", "auto")[self.provider.currentIndex()]
        providers = ["groq", "gemini"] if provider == "auto" else [provider]
        for selected in providers:
            task = NetworkTask(lambda name=selected: LLMService(self.config, self.memory_store, self.events).validate(name))
            task.finished.connect(lambda ok, text, name=selected: self.events.status.emit("LLM", ok, f"Ключ {name.title()} валиден" if ok else text))
            task.finished.connect(lambda ok, text, name=selected: self.events.log.emit(f"Проверка {name.title()}: {text}", "info" if ok else "error"))
            task.start(); self._task = task
            if selected == "groq":
                self.refresh_groq_models(silent=True)
            if selected == "gemini":
                self.refresh_gemini_models(silent=True)

    @Slot(bytes)
    def show_frame(self, data: bytes) -> None:
        image = QImage.fromData(data)
        if not image.isNull():
            self.last_frame_at = time.time()
            self.preview.setPixmap(QPixmap.fromImage(image).scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio))

    def load_memory(self) -> None:
        self.memory_table.setRowCount(0)
        for item in self.memory_store.all():
            row = self.memory_table.rowCount(); self.memory_table.insertRow(row)
            for col, value in enumerate((item["id"], item["author"], item["content"], item["created_at"])): self.memory_table.setItem(row, col, QTableWidgetItem(str(value)))

    def delete_memory(self) -> None:
        item = self.memory_table.currentItem()
        if item: self.memory_store.delete(int(self.memory_table.item(item.row(), 0).text())); self.load_memory()

    def closeEvent(self, event: Any) -> None:
        try:
            self.voice_stop.set()
            if self.voice_thread and self.voice_thread.isRunning():
                self.voice_thread.stop(); self.voice_thread.wait(2500)
            self.stop_bot()
            if self.obs: self.obs.stop(); self.obs.wait(2000)
            for task in list(self.model_tasks):
                if task.isRunning():
                    task.wait(1500)
            self.config.save(); backup = self.memory_store.backup()
            LOGGER.info("Application closed; memory backup=%s", backup)
        except Exception:
            LOGGER.exception("Shutdown failed")
        event.accept()


def main() -> int:
    LOGGER.info("Application started config=%s", CONFIG_PATH)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    app.setQuitOnLastWindowClosed(True)
    window = MainWindow()
    window.showNormal()
    window.raise_()
    window.activateWindow()
    LOGGER.info("Main window shown")
    QTimer.singleShot(250, lambda: (window.refresh_groq_models(silent=True), window.refresh_gemini_models(silent=True)))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
