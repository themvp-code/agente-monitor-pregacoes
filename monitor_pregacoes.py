#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de pregações do YouTube -> transcrição -> estratégia de conteúdo (Gemini).

Instalação:
    pip install -U google-genai youtube-transcript-api yt-dlp

Variáveis de ambiente:
    GEMINI_API_KEY    (obrigatória) chave da API do Google GenAI
    GEMINI_MODEL      (opcional)    padrão: gemini-2.5-flash
    YT_CHANNEL_IDS    (opcional)    IDs de canal separados por vírgula (UC...)
    YT_PROXY          (opcional)    ex.: http://user:pass@host:porta (útil se o IP for bloqueado)
    YT_COOKIES_FILE   (opcional)    cookies.txt (formato Netscape) para o yt-dlp

Uso:
    python monitor_pregacoes.py                          # verifica os canais de YT_CHANNEL_IDS uma vez
    python monitor_pregacoes.py --interval 30            # repete a cada 30 minutos
    python monitor_pregacoes.py --video URL_OU_ID [...]  # processa vídeos específicos
    python monitor_pregacoes.py --channel UCxxxx         # adiciona um canal extra
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional

from google import genai

def load_dotenv_file() -> None:
    """Carrega variáveis do arquivo .env (na pasta do script), sem dependências.
    Valores do .env têm prioridade sobre variáveis já definidas no terminal."""
    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        value = value.strip().strip('"').strip("'").strip()
        if name and value:
            os.environ[name] = value


load_dotenv_file()
# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
CHANNEL_IDS = [c.strip() for c in os.getenv("YT_CHANNEL_IDS", "").split(",") if c.strip()]
PROXY_URL = os.getenv("YT_PROXY") or None
COOKIES_FILE = os.getenv("YT_COOKIES_FILE") or None

PREFERRED_LANGS = ["pt", "pt-BR", "pt-PT"]  # ordem de preferência
ALLOW_ANY_LANGUAGE = True      # se não houver legenda em PT, usa qualquer idioma disponível
MIN_TRANSCRIPT_CHARS = 200     # abaixo disso a transcrição é considerada inválida
MAX_VIDEOS_PER_RUN = 5         # limite de vídeos novos por canal a cada verificação

STATE_FILE = Path("processed_videos.json")
OUTPUT_DIR = Path("estrategias")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

PROMPT_TEMPLATE = """Você é um estrategista de conteúdo digital especializado em ministérios cristãos.

Abaixo está a transcrição da pregação "{title}". Com base nela, crie uma estratégia de conteúdo completa em português do Brasil, contendo:

1. Resumo da mensagem (máx. 5 linhas) e versículos-chave citados.
2. Principais temas e ensinamentos, em tópicos.
3. 5 ideias de posts para Instagram/Facebook (formato, gancho e legenda sugerida).
4. 3 roteiros curtos para Reels/Shorts/TikTok (até 60s), com gancho nos 3 primeiros segundos.
5. 3 trechos da pregação com maior potencial de corte, com a justificativa.
6. 1 ideia de artigo ou devocional para blog/newsletter.
7. Sugestão de hashtags e calendário de publicação para 7 dias.

Seja fiel ao conteúdo da pregação, sem inventar citações nem doutrina que não esteja na fala.

TRANSCRIÇÃO:
{transcript}
"""

log = logging.getLogger("pregacoes")


class TranscriptUnavailable(RuntimeError):
    """Nenhuma estratégia conseguiu obter a transcrição do vídeo."""


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(value: str) -> str:
    """Aceita URL (watch, youtu.be, shorts, live, embed) ou o próprio ID."""
    value = value.strip()
    if _VIDEO_ID_RE.match(value):
        return value
    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower()
    if host.endswith("youtu.be"):
        candidate = parsed.path.strip("/").split("/")[0]
    elif "youtube.com" in host:
        qs = urllib.parse.parse_qs(parsed.query)
        if "v" in qs:
            candidate = qs["v"][0]
        else:
            parts = [p for p in parsed.path.split("/") if p]
            candidate = parts[1] if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed", "v"} else ""
    else:
        candidate = ""
    if not _VIDEO_ID_RE.match(candidate):
        raise ValueError(f"Não foi possível extrair o ID do vídeo de: {value!r}")
    return candidate


def http_get(url: str, timeout: int = 20) -> str:
    handler = urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}) if PROXY_URL \
        else urllib.request.ProxyHandler()
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5"}
    )
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean_text(text: str) -> str:
    """Remove marcações de som ([Música]), entidades HTML e espaços repetidos."""
    text = html.unescape(text)
    text = re.sub(r"\[[^\]]{1,40}\]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_-]+", "-", text)[:max_len].strip("-") or "video"


def get_video_title(video_id: str) -> str:
    """Título via oEmbed (leve e sem chave de API). Em falha, devolve o ID."""
    try:
        url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
            f"https://www.youtube.com/watch?v={video_id}", safe=""
        )
        return json.loads(http_get(url, timeout=10)).get("title") or video_id
    except Exception as exc:  # noqa: BLE001
        log.debug("oEmbed falhou para %s: %s", video_id, exc)
        return video_id


# --------------------------------------------------------------------------- #
# Extração de transcrição — camada 1: youtube-transcript-api (1.x e legada)
# --------------------------------------------------------------------------- #
def _join_snippets(items) -> str:
    parts = []
    for item in items:
        # 1.x devolve objetos (.text); versões antigas devolvem dicts (["text"])
        parts.append(item["text"] if isinstance(item, dict) else item.text)
    return " ".join(parts)


def _transcript_via_yta(video_id: str) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi

    # API nova (>= 1.0): get_transcript()/list_transcripts() foram REMOVIDOS.
    # Agora é preciso instanciar a classe e usar .list() / .fetch().
    if hasattr(YouTubeTranscriptApi, "fetch"):
        kwargs = {}
        if PROXY_URL:
            from youtube_transcript_api.proxies import GenericProxyConfig

            kwargs["proxy_config"] = GenericProxyConfig(http_url=PROXY_URL, https_url=PROXY_URL)
        api = YouTubeTranscriptApi(**kwargs)

        transcript_list = api.list(video_id)
        try:
            transcript = transcript_list.find_transcript(PREFERRED_LANGS)
        except Exception:  # NoTranscriptFound
            if not ALLOW_ANY_LANGUAGE:
                raise
            transcript = next(iter(transcript_list))
            log.info("Sem legenda em PT; usando idioma '%s'.", transcript.language_code)
        return _join_snippets(transcript.fetch())

    # API legada (< 1.0)
    if hasattr(YouTubeTranscriptApi, "get_transcript"):
        return _join_snippets(YouTubeTranscriptApi.get_transcript(video_id, languages=PREFERRED_LANGS))

    raise RuntimeError("Versão incompatível de youtube-transcript-api (sem fetch nem get_transcript).")


# --------------------------------------------------------------------------- #
# Camada 2: yt-dlp (a mais robusta — mantida ativamente contra mudanças do YouTube)
# --------------------------------------------------------------------------- #
def _parse_json3(raw: str) -> str:
    data = json.loads(raw)
    parts = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(seg.get("utf8", "") for seg in segs).replace("\n", " ").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def _parse_vtt(raw: str) -> str:
    lines, last = [], ""
    for line in raw.splitlines():
        line = line.strip()
        if (
            not line
            or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE"))
            or "-->" in line
            or line.isdigit()
        ):
            continue
        line = re.sub(r"<[^>]+>", "", line)  # remove tags <c>, <00:00:01.000>
        if line and line != last:            # legendas automáticas repetem linhas
            lines.append(line)
            last = line
    return " ".join(lines)


def _transcript_via_ytdlp(video_id: str) -> str:
    import yt_dlp

    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["pt.*"] + (["all", "-live_chat"] if ALLOW_ANY_LANGUAGE else []),
            "subtitlesformat": "json3/vtt/best",
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 3,
        }
        if PROXY_URL:
            opts["proxy"] = PROXY_URL
        if COOKIES_FILE:
            opts["cookiefile"] = COOKIES_FILE

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        files = [p for p in Path(tmp).glob(f"{video_id}.*") if p.suffix in {".json3", ".vtt"}]
        if not files:
            raise RuntimeError("yt-dlp não encontrou legendas para este vídeo.")

        def priority(p: Path) -> tuple:
            name = p.name.lower()
            lang_rank = 0 if ".pt" in name else 1                 # português primeiro
            fmt_rank = 0 if p.suffix == ".json3" else 1           # json3 é mais limpo
            return (lang_rank, fmt_rank, name)

        best = sorted(files, key=priority)[0]
        raw = best.read_text(encoding="utf-8", errors="replace")
        return _parse_json3(raw) if best.suffix == ".json3" else _parse_vtt(raw)


# --------------------------------------------------------------------------- #
# Camada 3: scraping leve da página do vídeo (menos confiável; último recurso)
# --------------------------------------------------------------------------- #
def _transcript_via_scraping(video_id: str) -> str:
    page = http_get(f"https://www.youtube.com/watch?v={video_id}&hl=pt-BR")
    match = re.search(r'"captionTracks":(\[.*?\])\s*,\s*"audioTracks"', page, re.DOTALL) or \
        re.search(r'"captionTracks":(\[.*?\])', page, re.DOTALL)
    if not match:
        raise RuntimeError("Página sem captionTracks (legendas desativadas ou bloqueio).")

    tracks = json.loads(match.group(1).replace("\\u0026", "&"))
    if not tracks:
        raise RuntimeError("Lista de legendas vazia.")

    def rank(track: dict) -> tuple:
        lang = track.get("languageCode", "")
        is_pt = 0 if lang.lower().startswith("pt") else 1
        is_auto = 1 if track.get("kind") == "asr" else 0
        return (is_pt, is_auto)

    track = sorted(tracks, key=rank)[0]
    if rank(track)[0] == 1 and not ALLOW_ANY_LANGUAGE:
        raise RuntimeError("Sem legenda em português.")

    raw = http_get(track["baseUrl"] + "&fmt=json3")
    if not raw.strip():
        raise RuntimeError("YouTube devolveu legenda vazia (possível exigência de PO token).")
    return _parse_json3(raw)


# --------------------------------------------------------------------------- #
# Orquestração da extração
# --------------------------------------------------------------------------- #
STRATEGIES: list[tuple[str, Callable[[str], str]]] = [
    ("youtube-transcript-api", _transcript_via_yta),
    ("yt-dlp", _transcript_via_ytdlp),
    ("scraping", _transcript_via_scraping),
]


def get_transcript(video_id: str) -> str:
    """Tenta cada estratégia em ordem e devolve a primeira transcrição válida."""
    errors: list[str] = []
    for name, func in STRATEGIES:
        try:
            text = clean_text(func(video_id))
            if len(text) >= MIN_TRANSCRIPT_CHARS:
                log.info("Transcrição obtida via %s (%d caracteres).", name, len(text))
                return text
            errors.append(f"{name}: texto curto demais ({len(text)} caracteres)")
        except ImportError as exc:
            errors.append(f"{name}: dependência ausente ({exc})")
        except Exception as exc:  # noqa: BLE001 - qualquer falha passa para a próxima camada
            msg = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
            errors.append(f"{name}: {type(exc).__name__}: {msg}")
            log.warning("Estratégia '%s' falhou para %s: %s", name, video_id, type(exc).__name__)
    raise TranscriptUnavailable(f"Sem transcrição para {video_id}. Detalhes: " + " | ".join(errors))


# --------------------------------------------------------------------------- #
# Monitoramento de canais (feed RSS público do YouTube)
# --------------------------------------------------------------------------- #
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


def parse_channel_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    videos = []
    for entry in root.findall("atom:entry", _NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=_NS)
        if not vid:
            continue
        videos.append({
            "id": vid,
            "title": entry.findtext("atom:title", default=vid, namespaces=_NS),
            "published": entry.findtext("atom:published", default="", namespaces=_NS),
        })
    return videos


def fetch_channel_videos(channel_id: str) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    return parse_channel_feed(http_get(url))


def load_state() -> set[str]:
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_state(processed: set[str]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(processed), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- #
# Gemini (lógica de chamada mantida: client.models.generate_content)
# --------------------------------------------------------------------------- #
def generate_strategy(client: genai.Client, title: str, transcript: str, retries: int = 3) -> str:
    prompt = PROMPT_TEMPLATE.format(title=title, transcript=transcript)
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("Resposta vazia do Gemini.")
            return text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            wait = 5 * attempt
            log.warning("Gemini falhou (tentativa %d/%d): %s. Aguardando %ds.", attempt, retries, exc, wait)
            if attempt < retries:
                time.sleep(wait)
    raise RuntimeError(f"Gemini falhou após {retries} tentativas: {last_exc}")


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def process_video(client: genai.Client, video_id: str, title: Optional[str] = None) -> Path:
    title = title or get_video_title(video_id)
    log.info("Processando: %s (%s)", title, video_id)

    transcript = get_transcript(video_id)
    strategy = generate_strategy(client, title, transcript)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / f"{video_id}_{slugify(title)}.md"
    out.write_text(
        f"# Estratégia de conteúdo — {title}\n\n"
        f"Vídeo: https://www.youtube.com/watch?v={video_id}\n\n---\n\n{strategy}\n",
        encoding="utf-8",
    )
    log.info("Estratégia salva em %s", out)
    return out


def run_once(client: genai.Client, channel_ids: list[str], extra_videos: list[str]) -> None:
    processed = load_state()

    queue: list[tuple[str, Optional[str]]] = []
    for value in extra_videos:
        queue.append((extract_video_id(value), None))

    for channel_id in channel_ids:
        try:
            videos = fetch_channel_videos(channel_id)
        except Exception as exc:  # noqa: BLE001
            log.error("Falha ao ler o feed do canal %s: %s", channel_id, exc)
            continue
        new = [v for v in videos if v["id"] not in processed][:MAX_VIDEOS_PER_RUN]
        log.info("Canal %s: %d vídeo(s) novo(s).", channel_id, len(new))
        queue.extend((v["id"], v["title"]) for v in reversed(new))  # mais antigos primeiro

    for video_id, title in queue:
        try:
            process_video(client, video_id, title)
            processed.add(video_id)
            save_state(processed)
        except TranscriptUnavailable as exc:
            # Não marca como processado: tenta de novo na próxima verificação
            # (legendas automáticas às vezes demoram a ficar disponíveis).
            log.error("%s", exc)
        except Exception as exc:  # noqa: BLE001
            log.error("Erro ao processar %s: %s", video_id, exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Pregações do YouTube -> estratégia de conteúdo (Gemini).")
    parser.add_argument("--video", nargs="+", default=[], help="URLs ou IDs de vídeos específicos.")
    parser.add_argument("--channel", nargs="+", default=[], help="IDs de canal extras (UC...).")
    parser.add_argument("--interval", type=float, default=0, help="Repetir a cada N minutos (0 = uma vez).")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if not GEMINI_API_KEY:
        log.error("Defina a variável de ambiente GEMINI_API_KEY.")
        return 1

    channels = CHANNEL_IDS + args.channel
    if not channels and not args.video:
        log.error("Informe --video/--channel ou defina YT_CHANNEL_IDS.")
        return 1

    client = genai.Client(api_key=GEMINI_API_KEY)

    try:
        while True:
            run_once(client, channels, args.video)
            args.video = []  # vídeos avulsos só são processados na primeira rodada
            if args.interval <= 0:
                break
            log.info("Próxima verificação em %.0f minuto(s).", args.interval)
            time.sleep(args.interval * 60)
    except KeyboardInterrupt:
        log.info("Interrompido pelo usuário.")
    return 0


if __name__ == "__main__":
    sys.exit(main())