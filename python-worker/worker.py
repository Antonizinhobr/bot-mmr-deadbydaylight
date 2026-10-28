import argparse
import difflib
import glob
import hashlib
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from multiprocessing import get_context

import cv2
import numpy as np

VERSAO = "3.6"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR") or os.path.join(BASE_DIR, "templates")

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    load_dotenv(os.path.join(BASE_DIR, "..", ".env"))
except ImportError:
    pass

log = logging.getLogger("dbd")

if not os.getenv("TESSDATA_PREFIX") and os.path.isfile(os.path.join(BASE_DIR, "tessdata", "por.traineddata")):
    os.environ["TESSDATA_PREFIX"] = os.path.join(BASE_DIR, "tessdata")


def _memoria_mb():
    for arq in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(arq).read().strip()
            if v.isdigit() and int(v) < 1 << 50:
                return int(v) // (1024 * 1024)
        except OSError:
            pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        return None


def _env(nome, padrao=None, tipo=str):
    v = os.getenv(nome)
    if v is None or v == "":
        return padrao
    if tipo is bool:
        return v.strip().lower() in ("1", "true", "sim", "yes", "on")
    try:
        return tipo(v)
    except ValueError:
        return padrao


SITE_PADRAO = "https://botmmr.discloud.dev"


class Config:
    def __init__(self):
        self.r2_account = _env("R2_ACCOUNT_ID")
        self.r2_key = _env("R2_ACCESS_KEY_ID")
        self.r2_secret = _env("R2_SECRET_ACCESS_KEY")
        self.r2_bucket = _env("R2_BUCKET_NAME", "dbd-mmr")
        self.r2_endpoint = _env("R2_ENDPOINT") or (
            f"https://{self.r2_account}.r2.cloudflarestorage.com" if self.r2_account else None)
        self.armazenamento = (_env("ARMAZENAMENTO") or ("r2" if os.getenv("R2_BUCKET_NAME") else "site")).lower()
        self.site_url = (_env("SITE_URL") or SITE_PADRAO).rstrip("/")
        self.worker_token = _env("WORKER_TOKEN")
        self.database_url = _env("DATABASE_URL")
        self.webhook = _env("DISCORD_WEBHOOK_URL")
        self.discord_token = _env("DISCORD_TOKEN")
        self.tesseract_cmd = _env("TESSERACT_CMD")
        self.fps = _env("FPS_AMOSTRA", 2.0, float)
        cpus = os.cpu_count() or 2
        mem = _memoria_mb()
        pela_ram = max(1, (mem - 700) // 450) if mem else 8
        self.processos = _env("PROCESSOS", int(max(1, min(8, cpus - 1, pela_ram))), int)
        self.intervalo_fila = _env("INTERVALO_FILA", 10, int)
        self.confianca_min = _env("CONFIANCA_MINIMA", 0.30, float)
        self.duracao_min = _env("DURACAO_MINIMA", 240, float)
        self.duracao_rejeitar = _env("DURACAO_REJEITAR", 60, float)
        self.max_24h = _env("MAX_PARTIDAS_24H", 15, int)
        self.max_gb = _env("MAX_TAMANHO_GB", 6.0, float)
        self.k = _env("K_FACTOR", 40.0, float)
        self.delta_max = _env("DELTA_MAX", 50.0, float)
        self.mmr_inicial = _env("MMR_INICIAL", 1500.0, float)
        self.rd_inicial = _env("RD_INICIAL", 350.0, float)
        self.apagar_video = _env("APAGAR_VIDEO", True, bool)
        self.mencionar = _env("MENCIONAR_JOGADOR", True, bool)
        self.exigir_snowflake = _env("EXIGIR_ID_DISCORD", False, bool)
        self.nome_servidor = _env("NOME_SERVIDOR", "Refúgio da Névoa")
        self.pasta_tmp = _env("PASTA_TEMP", os.path.join(BASE_DIR, "_tmp"))
        self.highlights = _env("HIGHLIGHTS", True, bool)
        self.highlights_max = _env("HIGHLIGHTS_MAX", 3, int)
        self.highlight_mb = _env("HIGHLIGHT_MAX_MB", 9.5, float)
        self.highlight_altura = _env("HIGHLIGHT_ALTURA", 720, int)
        self.highlight_seg_max = _env("HIGHLIGHT_SEG_MAX", 40, float)

    def faltando(self, para="fila"):
        fila = (["WORKER_TOKEN"] if self.armazenamento == "site"
                else ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"])
        req = {"fila": fila + ["DATABASE_URL", "DISCORD_WEBHOOK_URL"],
               "gravar": ["DATABASE_URL", "DISCORD_WEBHOOK_URL"],
               "discord": ["DISCORD_WEBHOOK_URL"]}[para]
        return [n for n in req if not os.getenv(n) and not (n == "R2_ACCOUNT_ID" and os.getenv("R2_ENDPOINT"))]


def configurar_log():
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    if not raiz.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(fmt)
        raiz.addHandler(h)
        try:
            fh = logging.handlers.RotatingFileHandler(os.path.join(BASE_DIR, "worker.log"),
                                                      maxBytes=5_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            raiz.addHandler(fh)
        except OSError:
            pass
    for ruidoso in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)


MAPA_SCORE = {
    "pallet_drop": ["PALLET_DROP"], "killer_stun": ["PALLET_STUN"],
    "vault": ["VAULT_FAST"],
    "dash_dodger": ["DEAD_HARD_DODGE"], "killer_blind": ["FLASHLIGHT_BLIND"],
    "escaped": ["CHASE_ESCAPED"],
    "survived": ["ESCAPE"], "obsession_escaped": ["ESCAPE"], "hatch_escape": ["ESCAPE"],
    "great_skill_check": ["SKILLCHECK_GREAT"], "repairs": ["GEN_PROGRESS"], "coop": ["GEN_COOP"],
    "heal": ["HEAL_POPUP"], "unhook": ["UNHOOK"], "safe_unhook": ["UNHOOK", "UNHOOK_SAFE"],
    "safe_hook_rescue": ["UNHOOK", "UNHOOK_SAFE"], "hook_rescue": ["UNHOOK"],
    "protection": ["PROTECTION_HIT"], "struggle": ["HOOK_STRUGGLE"], "wiggle": ["WIGGLE_POPUP"],
    "totem": ["TOTEM_CLEANSE"],
    "exhausted": ["STATUS_EXHAUSTED"], "endurance": ["STATUS_ENDURANCE"],
    "haste": ["STATUS_HASTE"], "broken": ["STATUS_BROKEN"],
    "good_skill_check": ["SKILLCHECK_GOOD"], "great_wiggle": ["WIGGLE_GREAT"], "good_wiggle": ["WIGGLE_GOOD"],
    "assist": ["TEAM_ASSIST"], "reunited": ["REUNITED"], "do_it_yourself": ["SELF_HEAL_DONE"],
    "fully_mended": ["MEND_DONE"], "basement_time": ["BASEMENT"],
}
BOLDNESS = {"PALLET_DROP", "PALLET_STUN", "VAULT_FAST", "DEAD_HARD_DODGE", "FLASHLIGHT_BLIND"}
BOLDNESS_BRUTO = {"bold", "distraction"}

MAPA_KILLER = {
    "k_survivor_found": ["K_FOUND"], "k_chase": ["K_CHASE_POPUP"], "k_hit": ["K_HIT"],
    "k_hook": ["K_HOOK"], "k_hook_novo": ["K_HOOK_NOVO"], "k_damage_generator": ["K_GEN_KICK"],
    "k_destruction": ["K_DESTRUCTION"], "k_bleeding": ["K_BLEEDING"], "k_entity_summoned": ["K_ENTITY"],
    "k_sacrifice": ["K_SACRIFICE"], "k_kill": ["K_KILL"], "k_bloodlust": ["K_BLOODLUST"],
    "k_no_one_escaped": ["K_NO_ONE_ESCAPED"], "k_blinded": ["K_BLINDED"], "k_stunned": ["K_STUNNED"],
    "k_flashlight_evasion": ["K_LIGHT_DODGE"], "k_flashlight_dodge": ["K_LIGHT_DODGE"],
}
PODER_KILLER = {
    "kp_rush": "efeito", "kp_rushing": "efeito", "kp_lethal_rush": "uso", "kp_lethal_rush_hit": "acerto",
    "kp_lethal_rush_down": "queda", "kp_speared": "acerto", "kp_reeling": "efeito", "kp_reeled_in": "efeito",
    "kp_sharpshooter": "efeito", "kp_violent_caster": "acerto", "kp_take_flight": "efeito", "kp_use_cantrip": "efeito",
    "kp_spectral_smash": "acerto", "kp_detected_dispelled": "efeito", "kp_chainsaw_sprint": "uso",
    "kp_curve": "efeito", "kp_chainsaw_hit": "queda", "kp_overdrive_hit": "acerto", "kp_overheat": "efeito",
    "kp_dream_snare": "uso", "kp_dream_projection": "efeito", "kp_dream_pallet": "efeito", "kp_dream_snare_hit": "acerto",
    "kp_dream_demon": "efeito", "kp_dream_master": "efeito", "kp_dream_stalker": "efeito",
}
for _k, _tipo in PODER_KILLER.items():
    MAPA_KILLER[_k] = {"uso": ["KP_USE"], "acerto": ["KP_HIT"], "queda": ["KP_HIT", "KP_DOWN"],
                       "efeito": ["KP_EFFECT"]}[_tipo]

CONJUNTOS = {
    ("sobrevivente", "en"): ["score", "sobrevivente/en/score"],
    ("sobrevivente", "pt"): ["sobrevivente/pt/score"],
    ("assassino", "en"): ["assassino/en/score"],
    ("assassino", "pt"): ["assassino/pt/score"],
}
VOTO_PAPEL = {
    "sobrevivente": {"bold", "vault", "repairs", "great_skill_check", "good_skill_check", "pallet_drop", "escaped",
                     "coop", "protection", "hook_rescue", "safe_hook_rescue", "survived", "recovery"},
    "assassino": {"k_survivor_found", "k_damage_generator", "k_hit", "k_bleeding", "k_hook", "k_destruction",
                  "k_hook_novo", "k_entity_summoned"},
}

EMBLEMAS = {
    "ENTITY DISPLEASED": ("assassino", "kills", 1), "ENTIDADE DESAGRADADA": ("assassino", "kills", 1),
    "BRUTAL KILLER": ("assassino", "kills", 2), "ASSASSINO BRUTAL": ("assassino", "kills", 2),
    "RUTHLESS KILLER": ("assassino", "kills", 3), "ASSASSINO IMPLACAVEL": ("assassino", "kills", 3),
    "MERCILESS KILLER": ("assassino", "kills", 4), "ASSASSINO IMPIEDOSO": ("assassino", "kills", 4),
    "ESCAPED": ("sobrevivente", "escapou", None), "ESCAPOU": ("sobrevivente", "escapou", None),
    "SACRIFICED": ("sobrevivente", "sacrificado", None), "SACRIFICADO": ("sobrevivente", "sacrificado", None),
    "KILLED": ("sobrevivente", "morto", None), "MORTO": ("sobrevivente", "morto", None),
    "DISCONNECTED": ("sobrevivente", "desconectou", None), "DESCONECTADO": ("sobrevivente", "desconectou", None),
}
TITULOS_PLACAR = ("SCOREBOARD", "PLACAR")
ROI_EMBLEMA = (40, 100, 760, 260)
LINHAS_PLACAR = [266, 388, 508, 628, 748]
STATUS_PLACAR = {"escapou": "escapou", "sacrificado": "morto", "morto": "morto", "gancho_bot": "morto",
                 "vivo": "vivo", "assassino": "assassino"}
CENTROS_PERKS = [(1784.3, 894.3), (1734.3, 944.3), (1834.3, 944.3), (1784.3, 994.3)]

MAPA_ACAO = {
    "recover": ("DYING_START", "DYING_END"), "repair": ("GEN_START", "GEN_END"),
    "heal": ("HEAL_TEAMMATE_START", "HEAL_TEAMMATE_END"), "heal_self": ("SELF_HEAL_START", "SELF_HEAL_END"),
    "being_healed": ("BEING_HEALED_START", "BEING_HEALED_END"), "unhook": ("UNHOOK_START", "UNHOOK_END"),
    "open_escape": ("EXIT_OPEN_START", "EXIT_OPEN_END"), "wiggle": ("CARRIED_START", "CARRIED_END"),
    "cleanse": ("TOTEM_START", "TOTEM_END"), "bless": ("TOTEM_START", "TOTEM_END"),
    "mend": ("MEND_START", "MEND_END"), "unlock": ("ACAO_OUTRA_START", "ACAO_OUTRA_END"),
    "insert_tape": ("ACAO_OUTRA_START", "ACAO_OUTRA_END"),
}
OCR_ACAO = {"RECOVER": "recover", "REPAIR": "repair", "HEAL": "heal", "HEALSELF": "heal_self",
            "BEINGHEALED": "being_healed", "UNHOOK": "unhook", "OPENESCAPE": "open_escape",
            "OPENEXITGATE": "open_escape", "WIGGLE": "wiggle", "CLEANSE": "cleanse", "BLESS": "bless",
            "MEND": "mend", "UNLOCK": "unlock", "INSERTTAPE": "insert_tape",
            "RECUPERAR": "recover", "REPARAR": "repair", "TRATAR": "heal", "CURAR": "heal",
            "TRATARSE": "heal_self", "CURARSE": "heal_self", "SENDOTRATADO": "being_healed",
            "SENDOCURADO": "being_healed", "TIRARDOGANCHO": "unhook", "DESENGANCHAR": "unhook",
            "ABRIRPORTAO": "open_escape", "ABRIR": "open_escape", "DEBATERSE": "wiggle", "DEBATER": "wiggle",
            "PURIFICAR": "cleanse", "PURIFICARTOTEM": "cleanse", "ABENCOAR": "bless", "ABENCOARTOTEM": "bless",
            "DESTRANCAR": "unlock"}

ROIS_BASE = {
    "score": ((1380, 15, 530, 300), "tr"),
    "status": ((1790, 380, 130, 330), "r"),
    "barra": ((700, 830, 520, 160), "b"),
    "gen_icone": ((40, 850, 180, 100), "bl"),
    "gen_digito": ((60, 878, 40, 48), "bl"),
    "gen_digito_k": ((118, 878, 40, 48), "bl"),
}
ESCALAS = [round(0.70 + 0.05 * i, 2) for i in range(19)]
W_BASE, H_BASE = 1920, 1080
LINHAS_SOBREVIVENTES = [608, 670, 732, 794]
LIMIAR_CHASE = 0.10


def _ancorar(bbox, ancora, s, W=W_BASE, H=H_BASE):
    x, y, w, h = bbox
    if ancora == "tr":
        x2, y2 = W - (W - x) * s, y * s
    elif ancora == "bl":
        x2, y2 = x * s, H - (H - y) * s
    elif ancora == "b":
        x2, y2 = W / 2 - (W / 2 - x) * s, H - (H - y) * s
    elif ancora == "r":
        x2, y2 = W - (W - x) * s, H / 2 - (H / 2 - y) * s
    else:
        x2, y2 = x * s, y * s
    x2, y2 = max(0, int(round(x2))), max(0, int(round(y2)))
    return x2, y2, int(round(min(w * s, W - x2))), int(round(min(h * s, H - y2)))


def _crop(img, bbox):
    x, y, w, h = bbox
    return img[y:y + h, x:x + w]


def _melhor(roi, tpl):
    if tpl is None or roi is None or tpl.size == 0 or tpl.shape[0] > roi.shape[0] or tpl.shape[1] > roi.shape[1]:
        return 0.0
    return float(cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED).max())


def _mascara_branca(bgr, v=165, s=70):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return (hsv[..., 2] > v) & (hsv[..., 1] < s)


_CAPTURA = {}


def _carregar_templates(pasta):
    meta_p = os.path.join(TEMPLATES_DIR, pasta, "_meta.json")
    try:
        meta = json.load(open(meta_p, encoding="utf-8")) if os.path.exists(meta_p) else {}
    except Exception:
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    _CAPTURA[pasta] = {k: float(v.get("escala", 1.0)) for k, v in meta.items() if isinstance(v, dict)}
    out = {}
    for f in sorted(glob.glob(os.path.join(TEMPLATES_DIR, pasta, "*.png"))):
        img = cv2.imread(f)
        if img is not None and img.size:
            out[os.path.splitext(os.path.basename(f))[0]] = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return out


def _escalar(tpls, s, captura=None):
    out = {}
    for k, v in tpls.items():
        f = s / (captura or {}).get(k, 1.0)
        out[k] = v if abs(f - 1.0) < 1e-3 else cv2.resize(
            v, None, fx=f, fy=f, interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC)
    return out


def _sem_sufixo(nome):
    return re.sub(r"_\d+$", "", nome)


def info_video(caminho):
    cap = cv2.VideoCapture(caminho)
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    ok = cap.isOpened()
    cap.release()
    dur = (n / fps) if fps else 0.0
    if shutil.which("ffprobe"):
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                  "-of", "default=nw=1:nk=1", caminho], capture_output=True, text=True, timeout=60)
            dur = float(out.stdout.strip() or dur)
        except Exception:
            pass
    return {"ok": ok and w > 0, "duracao": dur, "largura": w, "altura": h, "fps": fps}


def ler_frames(caminho, fps_amostra, inicio=0.0, duracao=None, so_keyframes=False):
    if shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-v", "error", "-nostdin"]
        cmd += ["-skip_frame", "nokey"] if so_keyframes else ["-skip_frame", "bidir"]
        if inicio > 0:
            cmd += ["-ss", f"{inicio:.3f}"]
        cmd += ["-i", caminho]
        if duracao:
            cmd += ["-t", f"{duracao:.3f}"]
        cmd += ["-an", "-sn", "-dn"]
        if so_keyframes:
            cmd += ["-vf", f"scale={W_BASE}:{H_BASE}", "-vsync", "vfr"]
        else:
            cmd += ["-vf", f"fps={fps_amostra},scale={W_BASE}:{H_BASE}"]
        cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        tam = W_BASE * H_BASE * 3
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=tam * 2)
        n = 0
        try:
            while True:
                buf = p.stdout.read(tam)
                if len(buf) < tam:
                    break
                yield inicio + n / fps_amostra, np.frombuffer(buf, np.uint8).reshape(H_BASE, W_BASE, 3)
                n += 1
        finally:
            try:
                p.stdout.close()
                p.kill()
                p.wait(timeout=10)
            except Exception:
                pass
        return
    cap = cv2.VideoCapture(caminho)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if inicio > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, inicio * 1000)
    passo = max(1, int(round(fps / fps_amostra))) if not so_keyframes else max(1, int(fps * 2))
    idx, fim = -1, (inicio + duracao) if duracao else None
    while cap.grab():
        idx += 1
        if idx % passo:
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break
        t = inicio + idx / fps
        if fim and t >= fim:
            break
        if frame.shape[1] != W_BASE or frame.shape[0] != H_BASE:
            frame = cv2.resize(frame, (W_BASE, H_BASE))
        yield t, frame
    cap.release()


def frame_em(caminho, t):
    if shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", caminho, "-frames:v", "1", "-an",
               "-vf", f"scale={W_BASE}:{H_BASE}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=30).stdout
        except Exception:
            return None
        tam = W_BASE * H_BASE * 3
        return np.frombuffer(out[:tam], np.uint8).reshape(H_BASE, W_BASE, 3) if len(out) >= tam else None
    cap = cv2.VideoCapture(caminho)
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, frame = cap.read()
    cap.release()
    return cv2.resize(frame, (W_BASE, H_BASE)) if ok else None


class _OCR:

    LOCAIS = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe"),
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Tesseract-OCR", "tesseract.exe"),
        "/usr/bin/tesseract", "/usr/local/bin/tesseract", "/opt/homebrew/bin/tesseract",
    ]

    def __init__(self, cmd=None):
        self.ok, self.motivo, self.caminho = False, "", None
        try:
            import pytesseract
        except ImportError:
            self.motivo = "falta o pacote Python: rode  pip install pytesseract"
            return
        cmd = cmd or os.getenv("TESSERACT_CMD")
        candidatos = [cmd] if cmd else [shutil.which("tesseract")] + self.LOCAIS
        for c in candidatos:
            if not c or not (os.path.isfile(c) or shutil.which(c)):
                continue
            pytesseract.pytesseract.tesseract_cmd = c
            try:
                pytesseract.get_tesseract_version()
            except Exception:
                continue
            self.pt, self.ok, self.caminho = pytesseract, True, c
            return
        self.motivo = (f"TESSERACT_CMD aponta para um arquivo que não existe: {cmd}" if cmd else
                       "programa tesseract não encontrado. Coloque no .env:  "
                       r"TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe")

    def texto(self, img_bin, cfg, lang=None):
        if not self.ok:
            return ""
        try:
            if lang:
                try:
                    return self.pt.image_to_string(img_bin, lang=lang, config=cfg).strip()
                except Exception:
                    pass
            return self.pt.image_to_string(img_bin, config=cfg).strip()
        except Exception:
            return ""

    @staticmethod
    def binarizar(bgr, fator=3, v=150, s=80):
        big = cv2.resize(bgr, None, fx=fator, fy=fator, interpolation=cv2.INTER_CUBIC)
        m = _mascara_branca(big, v, s).astype(np.uint8) * 255
        return cv2.copyMakeBorder(255 - m, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)


def _norm(txt):
    import unicodedata
    t = unicodedata.normalize("NFKD", (txt or "").upper())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9' ]", " ", t)).strip()


_PERKS_CACHE = {}


def _mascara_perks(s):
    if s in _PERKS_CACHE:
        return _PERKS_CACHE[s]
    pts = [(W_BASE - (W_BASE - x) * s, H_BASE - (H_BASE - y) * s) for x, y in CENTROS_PERKS]
    x0 = int(min(p[0] for p in pts) - 60 * s)
    y0 = int(min(p[1] for p in pts) - 60 * s)
    x1 = min(W_BASE, int(max(p[0] for p in pts) + 60 * s))
    y1 = min(H_BASE, int(max(p[1] for p in pts) + 60 * s))
    m = np.zeros((y1 - y0, x1 - x0), np.uint8)
    r = 39.3 * s
    for cx, cy in pts:
        cx, cy = cx - x0, cy - y0
        poly = np.array([[cx, cy - r], [cx + r, cy], [cx, cy + r], [cx - r, cy]], np.int32)
        cv2.polylines(m, [poly], True, 1, max(2, int(round(2 * s))))
    k = max(3, int(9 * s))
    faixa = (cv2.dilate(m, np.ones((k, k), np.uint8)) > 0) & (m == 0)
    _PERKS_CACHE[s] = ((x0, y0, x1, y1), m > 0, faixa)
    return _PERKS_CACHE[s]


def nota_perks(frame, s):
    (x0, y0, x1, y1), m, f = _mascara_perks(s)
    g = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    if g.shape != m.shape:
        return 0.0
    mag = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) + np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    melhor = -1.0
    for dy in (-4, 0, 4):
        for dx in (-4, 0, 4):
            mm, ff = np.roll(np.roll(m, dy, 0), dx, 1), np.roll(np.roll(f, dy, 0), dx, 1)
            a, b = float(mag[mm].mean()), float(mag[ff].mean())
            melhor = max(melhor, (a - b) / (a + b + 1.0))
    return melhor


def banner_mapa(frame):
    r = frame[912:951, 60:1000]
    g = cv2.cvtColor(r, cv2.COLOR_BGR2GRAY).astype(np.int16)
    sat = cv2.cvtColor(r, cv2.COLOR_BGR2HSV)[..., 1]
    viz = np.maximum(np.roll(g, 3, 0), np.roll(g, -3, 0))
    claro = (g > 85) & (sat < 120) & (g - viz > 18)
    claro[:3], claro[-3:] = False, False
    for y in range(claro.shape[0]):
        if claro[y].sum() < 200:
            continue
        tam, a, b = _maior_corrida(claro[y])
        a, b = max(0, a - 3), b - 3
        if b - a < 280 or not (20 <= a + 60 <= 160):
            continue
        if (claro[max(0, y - 3):y + 4, a:b].mean(1) > 0.6).sum() > 3:
            continue
        yy = 912 + y
        topo = frame[max(0, yy - 60):yy - 6, 60 + a:60 + b]
        tg = cv2.cvtColor(topo, cv2.COLOR_BGR2GRAY)
        ts = cv2.cvtColor(topo, cv2.COLOR_BGR2HSV)[..., 1]
        txt = ((tg > 200) & (ts < 50)).mean()
        if 0.02 < txt < 0.40:
            return yy, 60 + a, 60 + b
    return None


def emblema_vermelho(frame):
    x, y, w, h = ROI_EMBLEMA
    hsv = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2HSV)
    verm = ((hsv[..., 0] < 10) | (hsv[..., 0] > 170)) & (hsv[..., 1] > 150) & (hsv[..., 2] > 150)
    return float(verm.mean())


def ler_emblema(frame, ocr, lang):
    x, y, w, h = ROI_EMBLEMA
    r = frame[y:y + h, x:x + w]
    hsv = cv2.cvtColor(r, cv2.COLOR_BGR2HSV)
    verm = ((hsv[..., 0] < 10) | (hsv[..., 0] > 170)) & (hsv[..., 1] > 150) & (hsv[..., 2] > 150)
    m = 255 - cv2.resize(verm.astype(np.uint8) * 255, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    linhas = [_norm(l) for l in ocr.texto(m, "--psm 6", lang).split("\n")]
    linhas = [l for l in linhas if len(l) >= 5]
    for l in linhas:
        for tit in TITULOS_PLACAR:
            if difflib.SequenceMatcher(None, l, tit).ratio() >= 0.8 or tit in l:
                return {"tipo": "placar"}
        melhor, nota = None, 0.0
        for chave in EMBLEMAS:
            r2 = difflib.SequenceMatcher(None, l, chave).ratio()
            if r2 > nota:
                melhor, nota = chave, r2
        if melhor and nota >= 0.8:
            papel, campo, kills = EMBLEMAS[melhor]
            return {"tipo": "resultado", "papel": papel, "resultado": campo if papel == "sobrevivente" else "kills",
                    "kills": kills, "texto": melhor}
    return None


def ler_placar(frame, tpls):
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    linhas = []
    for y in LINHAS_PLACAR:
        reg = g[max(0, y - 30):y + 30, 140:225]
        notas = {}
        for k, t in tpls.items():
            kk = _sem_sufixo(k)
            notas[kk] = max(notas.get(kk, 0.0), _melhor(reg, t))
        k = max(notas, key=notas.get) if notas else None
        linhas.append({"status": STATUS_PLACAR.get(k) if k and notas[k] >= 0.6 else None,
                       "nota": round(notas.get(k, 0.0), 2) if k else 0.0, "bot": _etiqueta_bot(hsv, y)})
    return linhas


def _etiqueta_bot(hsv, y):
    b = hsv[max(0, y - 16):y + 16, 300:900]
    fundo = ((b[..., 0] >= 5) & (b[..., 0] <= 22) & (b[..., 1] > 110) & (b[..., 2] > 25) & (b[..., 2] < 90))
    letra = (b[..., 0] >= 5) & (b[..., 0] <= 25) & (b[..., 1] > 60) & (b[..., 2] > 150)
    m = cv2.morphologyEx((fundo | letra).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(m)
    for i in range(1, n):
        x, yy, w, h, a = st[i]
        if 32 <= w <= 75 and 14 <= h <= 30 and a >= 0.55 * w * h and 30 <= letra[yy:yy + h, x:x + w].sum() <= 300:
            return True
    return False


def _maior_corrida(linha):
    v = cv2.dilate(linha.astype(np.uint8).reshape(1, -1), np.ones((1, 7), np.uint8)).ravel() > 0
    melhor, ini = (0, 0, 0), None
    for i, x in enumerate(list(v) + [False]):
        if x and ini is None:
            ini = i
        elif not x and ini is not None:
            if i - ini > melhor[0]:
                melhor = (i - ini, ini, i)
            ini = None
    return melhor


class DetectorHUD:
    def __init__(self, fps_amostra=2.0, limiar=0.80, tesseract_cmd=None):
        self.fps_amostra = float(fps_amostra)
        self.limiar = limiar
        self.tesseract_cmd = tesseract_cmd
        self.ocr = _OCR(tesseract_cmd)
        self.conjuntos = {}
        for chave, pastas in CONJUNTOS.items():
            tpls, cap = {}, {}
            for i, pasta in enumerate(pastas):
                imgs = _carregar_templates(pasta)
                for k, v in imgs.items():
                    nome = k if k not in tpls else f"{_sem_sufixo(k)}_{50 + 10 * i + len(tpls)}"
                    tpls[nome] = v
                    cap[nome] = _CAPTURA.get(pasta, {}).get(k, 1.0)
            if tpls:
                self.conjuntos[chave] = (tpls, cap)
        if not self.conjuntos:
            raise RuntimeError(f"Templates não encontrados em {TEMPLATES_DIR} — copie a pasta templates/.")
        self.base = {
            "status": _carregar_templates("status"),
            "acao": _carregar_templates("acao"),
            "gen": {k: v for k, v in _carregar_templates("generator").items()
                    if k in ("gen_icone_contador", "portoes_energizados")},
            "placar": _carregar_templates("comum/placar"),
        }
        m = cv2.imread(os.path.join(TEMPLATES_DIR, "survivor", "chase_colchetes_mask.png"), 0)
        self.mascara_chase = (m > 0).astype(np.uint8) if m is not None else None
        self.escala_hud = 1.0
        self.fonte_chase = "estimado"
        self.info_chase = {}
        self.confianca_geral = 0.0
        self.duracao_s = 0.0
        self.brutos = []
        self.votos_papel = {}
        self.definir_conjunto("sobrevivente", "en")
        self._preparar(1.0)

    def definir_conjunto(self, papel, idioma):
        chave = (papel, idioma) if (papel, idioma) in self.conjuntos else next(iter(self.conjuntos))
        self.papel, self.idioma = chave
        self.base["score"], self.captura_score = self.conjuntos[chave]
        self.lang_ocr = "por" if self.idioma == "pt" else "eng"
        self.mapa_score = MAPA_KILLER if self.papel == "assassino" else MAPA_SCORE

    def calibrar(self, caminho, amostras=70, max_recortes=30):
        log.info("Calibrando a HUD e descobrindo papel/idioma (leva 1–3 min)...")
        recortes = []
        roi = _ancorar(*ROIS_BASE["score"], 1.0)
        roi_largo = (max(0, roi[0] - 200), roi[1], min(W_BASE, roi[2] + 200), min(H_BASE, int(roi[3] * 1.5)))
        dur = info_video(caminho)["duracao"] or 0
        for t in np.linspace(dur * 0.03, dur * 0.97, amostras) if dur > 5 else [0.0]:
            frame = frame_em(caminho, float(t))
            if frame is None:
                continue
            r = _crop(frame, roi_largo)
            if _mascara_branca(r).sum() > 150:
                recortes.append(cv2.cvtColor(r, cv2.COLOR_BGR2GRAY))
        todos_recortes = recortes
        recortes = recortes[:: max(1, len(recortes) // max_recortes)][:max_recortes]
        if len(recortes) < 3:
            log.warning("Calibração: poucos frames com pontuação (%d); usando escala 1.00", len(recortes))
            return 1.0, self.papel, self.idioma
        votantes = {}
        for (papel, idioma), (tpls, cap) in self.conjuntos.items():
            for k, v in tpls.items():
                if _sem_sufixo(k) in VOTO_PAPEL[papel]:
                    votantes[(papel, idioma, k)] = (v, cap.get(k, 1.0))
        preferidos = ("bold", "vault", "great_skill_check", "escaped", "k_survivor_found", "k_hook", "k_bleeding")
        regua, usados = {}, set()
        for chave in sorted(votantes, key=lambda c: (abs(votantes[c][1] - 1.0) < 1e-3, c[2] != _sem_sufixo(c[2]), c[2])):
            papel, idioma, k = chave
            base = _sem_sufixo(k)
            if base in preferidos and (papel, idioma, base) not in usados:
                usados.add((papel, idioma, base))
                regua[chave] = votantes[chave]

        def escalados(s, fator=1.0, todos=False):
            out = {}
            for chave, (v, c) in (votantes if todos else regua).items():
                f = s / c * fator
                if abs(f - 1.0) > 1e-3:
                    v = cv2.resize(v, None, fx=f, fy=f, interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC)
                if v.shape[0] >= 4 and v.shape[1] >= 8:
                    out[chave] = v
            return out

        def nota(s):
            tp = escalados(s)
            vals = sorted(max((_melhor(r, t) for t in tp.values()), default=0.0) for r in recortes)
            return float(np.mean(vals[len(vals) // 2:]))

        notas = {s: nota(s) for s in ESCALAS}
        grossa = max(notas, key=notas.get)
        for d in (-0.04, -0.03, -0.02, -0.01, 0.01, 0.02, 0.03, 0.04):
            s = round(grossa + d, 2)
            if 0.6 <= s <= 1.7 and s not in notas:
                notas[s] = nota(s)
        melhor = max(notas, key=notas.get)
        escala = melhor if notas[melhor] >= 0.55 else 1.0
        tp = escalados(escala, todos=True)
        votos = {}
        for r in todos_recortes:
            por_conj = {}
            for (papel, idioma, k), t in tp.items():
                v = _melhor(r, t)
                if v >= 0.75:
                    por_conj[(papel, idioma)] = max(por_conj.get((papel, idioma), 0.0), v)
            if por_conj:
                ganhador = max(por_conj, key=por_conj.get)
                votos[ganhador] = votos.get(ganhador, 0) + 1
        self.votos_papel = {f"{p}/{i}": n for (p, i), n in votos.items()}
        papel, idioma = (max(votos, key=votos.get) if votos else (self.papel, self.idioma))
        if escala != 1.0 and (papel, idioma) in self.conjuntos:
            escala = self._refinar_escala(todos_recortes, escala, *self.conjuntos[(papel, idioma)])
        log.info("Escala da HUD: %.2fx (nota %.2f) · papel: %s · idioma: %s · votos %s",
                 escala, notas[melhor], papel, idioma, self.votos_papel or "nenhum")
        return escala, papel, idioma

    def _refinar_escala(self, recortes, escala, tpls, cap, raio=0.05):
        def escalar(s):
            out = []
            for k, v in tpls.items():
                f = s / cap.get(k, 1.0)
                if abs(f - 1.0) > 1e-3:
                    v = cv2.resize(v, None, fx=f, fy=f, interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC)
                if v.shape[0] >= 6 and v.shape[1] >= 20:
                    out.append(v)
            return out
        base = escalar(escala)
        cand = sorted(recortes, key=lambda r: -max((_melhor(r, t) for t in base), default=0.0))[:20]
        melhor, chave = escala, (-1, -1.0)
        for s in np.round(np.arange(escala - raio, escala + raio + 1e-6, 0.01), 2):
            tp = escalar(float(s))
            notas = [max((_melhor(r, t) for t in tp), default=0.0) for r in cand]
            k = (sum(1 for v in notas if v >= self.limiar), float(np.mean(notas)) if notas else 0.0)
            if k > chave:
                melhor, chave = float(s), k
        log.info("Escala refinada: %.2fx (%d de %d recortes reconhecidos)", melhor, chave[0], len(cand))
        return melhor

    def calibrar_escala(self, caminho):
        return self.calibrar(caminho)[0]

    def _preparar(self, s):
        self.escala_hud = s
        pastas = {"status": "status", "acao": "acao", "gen": "generator", "placar": "comum/placar"}
        self.tpl = {k: _escalar(v, s, _CAPTURA.get(pastas.get(k, k))) for k, v in self.base.items()
                    if k not in ("score", "placar")}
        self.tpl["score"] = _escalar(self.base["score"], s, self.captura_score)
        self.tpl["placar"] = self.base["placar"]
        self.tpl_meia = {k: cv2.resize(v, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
                         for k, v in self.tpl["score"].items() if v.shape[0] >= 8 and v.shape[1] >= 16}
        self.roi = {k: _ancorar(b, a, s) for k, (b, a) in ROIS_BASE.items()}
        self.chase_mask = None
        if self.mascara_chase is not None:
            mk = cv2.resize(self.mascara_chase, None, fx=s, fy=s, interpolation=cv2.INTER_NEAREST) > 0
            faixa = (cv2.dilate(mk.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0) & ~mk
            self.chase_mask = (mk, faixa)
            self.linhas_y = [int(round(H_BASE - (H_BASE - y) * s)) - int(6 * s) for y in LINHAS_SOBREVIVENTES]
            self.linha_x = int(round(48 * s))

    def hud_presente(self, frame, g=None):
        if nota_perks(frame, self.escala_hud) >= 0.10:
            return True
        g = g if g is not None else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        roi_gen = _crop(g, self.roi["gen_icone"])
        return max((_melhor(roi_gen, tp) for tp in self.tpl["gen"].values()), default=0) >= 0.6

    def _colchetes(self, frame):
        if self.chase_mask is None:
            return None
        mk, faixa = self.chase_mask
        h, w = mk.shape
        notas = []
        for y in self.linhas_y:
            reg = frame[max(0, y - 6):y + h + 6, max(0, self.linha_x - 6):self.linha_x + w + 6]
            if reg.shape[0] < h or reg.shape[1] < w:
                notas.append(0.0)
                continue
            br = _mascara_branca(reg, 150, 70).astype(np.float32)
            melhor = -1.0
            for dy in range(0, reg.shape[0] - h + 1, 2):
                for dx in range(0, reg.shape[1] - w + 1, 2):
                    b = br[dy:dy + h, dx:dx + w]
                    v = float(b[mk].mean() - b[faixa].mean())
                    if v > melhor:
                        melhor = v
            notas.append(round(melhor, 3))
        return notas

    def _ler_score(self, roi_cinza):
        rm = cv2.resize(roi_cinza, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        achados = []
        for k, tm in self.tpl_meia.items():
            if tm.shape[0] > rm.shape[0] or tm.shape[1] > rm.shape[1] or tm.size == 0:
                continue
            r = cv2.matchTemplate(rm, tm, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(r >= 0.50)
            if not len(ys):
                continue
            tf = self.tpl["score"][k]
            for i in np.argsort(r[ys, xs])[::-1][:3]:
                x, y = int(xs[i]) * 2, int(ys[i]) * 2
                x0, y0 = max(0, x - 6), max(0, y - 6)
                jan = roi_cinza[y0:y + tf.shape[0] + 6, x0:x + tf.shape[1] + 6]
                if jan.shape[0] < tf.shape[0] or jan.shape[1] < tf.shape[1]:
                    continue
                rr = cv2.matchTemplate(jan, tf, cv2.TM_CCOEFF_NORMED)
                _, v, _, loc = cv2.minMaxLoc(rr)
                if v >= self.limiar:
                    achados.append((_sem_sufixo(k), x0 + loc[0], y0 + loc[1], tf.shape[1], tf.shape[0], v))
                    break
        return self._filtrar_palavras(achados, roi_cinza)

    def _palavra_continua(self, roi, x, y, w, h):
        s = self.escala_hud
        pad = max(2, int(round(3 * s)))
        faixa = roi[y + pad:y + h - pad]
        if faixa.size == 0:
            return False
        txt = faixa[:, max(0, x + pad):x + w - pad]
        corte = max(120.0, float(np.percentile(txt, 90)) * 0.80) if txt.size else 255.0
        lo, hi = max(0, x - int(30 * s)), min(roi.shape[1], x + w + int(30 * s))
        cols = (faixa[:, lo:hi] > corte).sum(0) >= 1
        a0, b0 = x + pad - lo, x + w - pad - lo
        m = int(round(2 * s))
        dentro = np.where(cols[max(0, a0 - m):max(0, b0 + m)])[0]
        if not len(dentro):
            return False
        ta, tb = int(dentro[0]) + max(0, a0 - m), int(dentro[-1]) + max(0, a0 - m)
        gap = max(2, int(round(3 * s)))
        j, vazio = tb, 0
        while j + 1 < len(cols):
            j += 1
            if cols[j]:
                tb, vazio = j, 0
            else:
                vazio += 1
                if vazio > gap:
                    break
        j, vazio = ta, 0
        while j - 1 >= 0:
            j -= 1
            if cols[j]:
                ta, vazio = j, 0
            else:
                vazio += 1
                if vazio > gap:
                    break
        lim = int(round(3 * s))
        return (tb - b0) > lim or (a0 - ta) > lim

    def _filtrar_palavras(self, achados, roi):
        s = self.escala_hud
        achados = sorted(achados, key=lambda a: -a[3])
        aceitos = []
        for k, x, y, w, h, v in achados:
            dentro = any(x >= bx - 4 * s and x + w <= bx + bw + 4 * s and abs(y - by) <= 0.6 * h
                         for _, bx, by, bw, bh, _ in aceitos)
            if dentro:
                continue
            if self._palavra_continua(roi, x, y, w, h):
                continue
            aceitos.append((k, x, y, w, h, v))
        vistos = {a[0] for a in aceitos}
        if "obsession_escaped" in vistos:
            vistos.discard("escaped")
        if "safe_unhook" in vistos or "safe_hook_rescue" in vistos:
            vistos.discard("unhook")
            vistos.discard("hook_rescue")
            vistos.discard("safe_unhook") if "safe_hook_rescue" in vistos else None
        return vistos

    def _achar_barra(self, frame):
        s = self.escala_hud
        x0, y0, w, h = self.roi["barra"]
        g = cv2.cvtColor(frame[y0:y0 + h, x0:x0 + w], cv2.COLOR_BGR2GRAY).astype(np.float32)
        borda = np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) > 40
        cand = np.where(borda.sum(1) >= 150 * s)[0]
        longas = {}
        for a in cand:
            run = _maior_corrida(borda[a])
            if run[0] >= 135 * s:
                longas[a] = run
        for a, (_, s1, e1) in longas.items():
            for b, (_, s2, e2) in longas.items():
                if 5 * s <= b - a <= 16 * s and abs(s1 - s2) <= 12 * s and abs(e1 - e2) <= 25 * s:
                    return x0 + s1, y0 + int(a)
        return None

    def _ler_rotulo(self, frame, xl, yt, usar_ocr):
        s = self.escala_hud
        reg = frame[max(0, int(yt - 24 * s)):int(yt + 1), max(0, int(xl - 14 * s)):int(xl + 270 * s)]
        if reg.size == 0:
            return None, 0.0, ""
        g = cv2.cvtColor(reg, cv2.COLOR_BGR2GRAY)
        notas = {}
        for k, t in self.tpl["acao"].items():
            kk = _sem_sufixo(k)
            notas[kk] = max(notas.get(kk, 0.0), _melhor(g, t))
        k = max(notas, key=notas.get) if notas else None
        nota = notas.get(k, 0.0)
        txt = ""
        if usar_ocr and self.ocr.ok and nota < 0.75:
            faixa = frame[int(yt - 19 * s):int(yt - 2 * s), max(0, int(xl - 6 * s)):int(xl + 254 * s)]
            if faixa.size:
                txt = self.ocr.texto(self.ocr.binarizar(faixa),
                                     "--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ()-",
                                     self.lang_ocr)
        return k, nota, txt.replace(" ", "")

    @staticmethod
    def _rotulo_por_votos(votos):
        placar = {}
        for k, nota, txt in votos:
            if k and nota >= 0.65:
                placar[k] = placar.get(k, 0) + nota
            if txt:
                chave = max(OCR_ACAO, key=lambda c: difflib.SequenceMatcher(None, txt[:len(c) + 2], c).ratio())
                r = difflib.SequenceMatcher(None, txt[:len(chave) + 2], chave).ratio()
                if r >= 0.72:
                    placar[OCR_ACAO[chave]] = placar.get(OCR_ACAO[chave], 0) + r
        return max(placar, key=placar.get) if placar else None

    def _nome_mapa(self, frame, banner):
        if not self.ocr.ok or not banner:
            return None
        yy, xa, xb = banner
        txt = " ".join(self.ocr.texto(self.ocr.binarizar(frame[max(0, yy - 56):yy - 4, max(0, xa - 40):min(W_BASE, xb + 60)], 3, 180, 60),
                                      "--psm 7", self.lang_ocr).split())
        txt = re.sub(r"[^A-Za-zÀ-ÿ' -]", "", txt).strip(" -'")
        letras = sum(ch.isalpha() or ch in " -'" for ch in txt)
        if len(txt) < 5 or letras / max(1, len(txt)) < 0.85:
            return None
        return " ".join(w[:1].upper() + w[1:].lower() for w in txt.split())

    def _digito_gens(self, frame, roi=None):
        r = _crop(frame, roi or self.roi["gen_digito"])
        if r.size == 0 or not self.ocr.ok:
            return None
        g = cv2.resize(cv2.cvtColor(r, cv2.COLOR_BGR2GRAY), None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        g = cv2.createCLAHE(2.0, (4, 4)).apply(g)
        _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if b.mean() < 127:
            b = 255 - b
        t = self.ocr.texto(cv2.copyMakeBorder(b, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255),
                           "--psm 10 -c tessedit_char_whitelist=012345")
        return int(t[0]) if t and t[0].isdigit() else None

    def analisar_trecho(self, caminho, inicio=0.0, duracao=None, progresso=None):
        eventos, brutos = [], []
        ultimo, status_ativos = {}, set()
        leituras, gens = [], None
        passo_gens = max(1, int(round(2 * self.fps_amostra)))
        ultimo_digito = None
        acao, sem_barra, em_banner = None, 0, False
        n, n_hud, t = 0, 0, inicio
        amostras_chase = []
        prox_log = inicio + 120.0
        ult_emblema, ult_resultado, ult_placar = -99.0, -999.0, -999.0
        sobrevivente = self.papel == "sobrevivente"
        roi_digito = self.roi["gen_digito" if sobrevivente else "gen_digito_k"]

        def fechar(a):
            rot = self._rotulo_por_votos(a["votos"])
            if rot is None:
                return []
            t0, t1 = round(a["t0"], 2), round(a["ultimo"] + 0.5 / self.fps_amostra, 2)
            brutos.append({"t": t0, "label": f"acao:{rot}", "fim": t1})
            out = [{"t": t0, "tipo": "ACAO_START", "acao": rot}, {"t": t1, "tipo": "ACAO_END", "acao": rot}]
            par = MAPA_ACAO.get(rot)
            if par:
                out += [{"t": t0, "tipo": par[0]}, {"t": t1, "tipo": par[1]}]
            if rot == "recover":
                out += [{"t": t0, "tipo": "DOWNED"}, {"t": t0, "tipo": "HIT_RECEIVED"}]
            if rot == "unhook" and t1 - t0 >= 0.4:
                out.append({"t": t1, "tipo": "UNHOOK"})
            return out

        for t, frame in ler_frames(caminho, self.fps_amostra, inicio, duracao):
            n += 1
            if progresso and t >= prox_log:
                progresso(t)
                prox_log += 120.0
            g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            hud = self.hud_presente(frame, g)
            b = banner_mapa(frame)
            if b:
                if not em_banner:
                    eventos.append({"t": round(t, 2), "tipo": "MATCH_BANNER", "mapa": self._nome_mapa(frame, b)})
                em_banner = True
            else:
                em_banner = False
            if hud:
                n_hud += 1
            else:
                if self.ocr.ok and t - ult_emblema >= 1.0 and emblema_vermelho(frame) > 0.003:
                    ult_emblema = t
                    e = ler_emblema(frame, self.ocr, self.lang_ocr)
                    if e and e["tipo"] == "resultado" and t - ult_resultado > 60:
                        ult_resultado = t
                        eventos.append({"t": round(t, 2), "tipo": "MATCH_RESULT", "papel": e["papel"],
                                        "resultado": e["resultado"], "kills": e["kills"], "texto": e["texto"]})
                    elif e and e["tipo"] == "placar" and t - ult_placar > 60:
                        linhas = ler_placar(frame, self.tpl["placar"])
                        if sum(1 for l in linhas if l["status"]) >= 4:
                            ult_placar = t
                            eventos.append({"t": round(t, 2), "tipo": "PLACAR", "linhas": linhas})

            notas_chase = self._colchetes(frame) if (hud and sobrevivente) else None
            if notas_chase is not None:
                amostras_chase.append((round(t, 2), notas_chase))

            roi_c = _crop(frame, self.roi["score"])
            if _mascara_branca(roi_c).sum() > 120 * self.escala_hud ** 2:
                for k in self._ler_score(cv2.cvtColor(roi_c, cv2.COLOR_BGR2GRAY)):
                    if t - ultimo.get(k, -99) > 3.0:
                        brutos.append({"t": round(t, 2), "label": k})
                        for tipo in self.mapa_score.get(k, []):
                            eventos.append({"t": round(t, 2), "tipo": tipo})
                        if sobrevivente and k in BOLDNESS_BRUTO:
                            eventos.append({"t": round(t, 2), "tipo": "_BOLD"})
                    ultimo[k] = t
            if not hud:
                if acao is not None:
                    sem_barra += 1
                    if sem_barra >= 2:
                        eventos += fechar(acao)
                        acao = None
                continue

            if sobrevivente:
                barra = self._achar_barra(frame)
                if barra:
                    sem_barra = 0
                    usar_ocr = acao is None or len(acao["votos"]) < 4
                    voto = self._ler_rotulo(frame, barra[0], barra[1], usar_ocr)
                    if acao is None:
                        acao = {"t0": t, "votos": [voto], "ultimo": t}
                    else:
                        acao["votos"].append(voto)
                        acao["ultimo"] = t
                elif acao is not None:
                    sem_barra += 1
                    if sem_barra >= 2:
                        eventos += fechar(acao)
                        acao = None

                if n % 2 == 0 and self.tpl["status"]:
                    roi_s = _crop(g, self.roi["status"])
                    agora = {k for k, tp in self.tpl["status"].items() if _melhor(roi_s, tp) >= self.limiar}
                    for k in agora - status_ativos:
                        brutos.append({"t": round(t, 2), "label": k + "_on"})
                    status_ativos = agora

            if self.ocr.ok and n % passo_gens == 0:
                rd = cv2.resize(_crop(g, roi_digito), (20, 24))
                mudou = ultimo_digito is None or float(np.abs(rd.astype(np.int16) - ultimo_digito).mean()) > 6
                if mudou or len(leituras) < 4:
                    d = self._digito_gens(frame, roi_digito)
                    ultimo_digito = rd.astype(np.int16)
                    if d is not None:
                        leituras = (leituras + [d])[-7:]
                        moda = max(set(leituras), key=leituras.count)
                        if leituras.count(moda) >= 4 and moda != gens:
                            eventos.append({"t": round(t, 2), "tipo": "GENS_RESTANTES", "valor": moda})
                            gens = moda
        if acao is not None:
            eventos += fechar(acao)
        return {"eventos": eventos, "brutos": brutos, "n": n, "n_hud": n_hud, "fim": t, "chase": amostras_chase}

    def analisar(self, caminho, processos=1, progresso=None):
        info = info_video(caminho)
        dur = info["duracao"]
        escala, papel, idioma = self.calibrar(caminho)
        self.definir_conjunto(papel, idioma)
        self._preparar(escala)
        if processos > 1 and dur > 180:
            n = int(min(processos, max(1, dur // 90)))
            lim = [(i * dur / n, dur / n + 1.0) for i in range(n)]
            log.info("Analisando %.0f s de vídeo em %d processos (sem progresso na tela até terminar)...", dur, n)
            ctx = get_context("spawn")
            with ProcessPoolExecutor(n, mp_context=ctx) as ex:
                futs = [ex.submit(_trabalho_trecho, caminho, a, d, self.escala_hud, self.fps_amostra,
                                  self.limiar, self.tesseract_cmd, self.papel, self.idioma) for a, d in lim]
                partes = [f.result() for f in futs]
        else:
            log.info("Analisando %.0f s de vídeo...", dur)
            partes = [self.analisar_trecho(caminho, 0.0, None,
                                           progresso or (lambda t: log.info("   ... %.0f / %.0f s", t, dur)))]
        eventos = [e for p in partes for e in p["eventos"]]
        self.brutos = [b for p in partes for b in p["brutos"]]
        self.duracao_s = round(max([dur] + [p["fim"] for p in partes]), 2)
        self.confianca_geral = round(sum(p["n_hud"] for p in partes) / max(1, sum(p["n"] for p in partes)), 2)
        eventos = sorted(eventos, key=lambda e: e["t"])
        eventos = self._dedupe_fronteiras(eventos)
        self._confirmar_papel(eventos)
        amostras = sorted({a[0]: a for p in partes for a in p.get("chase", [])}.values())
        if self.papel == "assassino":
            ev_chase, self.info_chase = self._chases_assassino(eventos)
            self.fonte_chase = "feed"
            return sorted(eventos + ev_chase, key=lambda e: e["t"])
        eventos = self._unhook_unico(eventos)
        eventos = self._stun_implica_drop(eventos)
        ev_chase, self.info_chase = self._chases_por_colchete(amostras, eventos)
        estimado = self._inferir_chases(eventos, self.duracao_s)
        base = [e for e in estimado if e["tipo"] not in ("CHASE_START", "CHASE_END")]
        ints_est = _intervalos(estimado, "CHASE_START", "CHASE_END")
        ints_hud = _intervalos(ev_chase, "CHASE_START", "CHASE_END")
        self.fonte_chase = "hud" if self.info_chase["chase_pov_s"] >= 10 else "estimado"
        quedas = sorted(e["t"] for e in eventos if e["tipo"] == "DOWNED")
        fugas = sorted(e["t"] for e in eventos if e["tipo"] == "CHASE_ESCAPED")
        provas = sorted(e["t"] for e in eventos if e["tipo"] in BOLDNESS or e["tipo"] in
                        ("_BOLD", "CHASE_ESCAPED", "DOWNED", "HIT_RECEIVED", "PROTECTION_HIT"))
        ints_hud = [(a, b) for a, b in ints_hud if any(a - 3.0 <= t <= b + 3.0 for t in provas)]
        finais = self._juntar_chases(ints_hud, ints_est, quedas, fugas)
        self.info_chase["chase_final_s"] = round(sum(b - a for a, b in finais), 1)
        self.info_chase["intervalos_hud"] = [(round(a, 1), round(b, 1)) for a, b in ints_hud]
        self.info_chase["intervalos_estimados"] = [(round(a, 1), round(b, 1)) for a, b in ints_est]
        self.info_chase["chase_hud_s"] = round(sum(b - a for a, b in ints_hud), 1)
        self.info_chase["chase_pov_s"] = self.info_chase["chase_hud_s"]
        self.info_chase["chase_estimado_s"] = round(sum(b - a for a, b in ints_est), 1)
        chase_ev = [x for a, b in finais for x in ({"t": round(a, 2), "tipo": "CHASE_START"},
                                                   {"t": round(b, 2), "tipo": "CHASE_END"})]
        extra = [e for e in ev_chase if not e["tipo"].startswith("CHASE_")]
        return sorted(base + chase_ev + extra, key=lambda e: e["t"])

    @staticmethod
    def _juntar_chases(ints_hud, ints_est, quedas, fugas, gap=4.0, minimo=3.0):
        cortes = sorted([(t, "fuga") for t in fugas] + [(t, "queda") for t in quedas])

        def partir(a, b):
            pedacos, ini, tipo_ini = [], a, None
            for t, tipo in cortes:
                if ini + 0.5 < t < b - 0.5:
                    pedacos.append((ini, t, tipo_ini))
                    ini, tipo_ini = t, tipo
            pedacos.append((ini, b, tipo_ini))
            return pedacos

        brutos = []
        for a, b in sorted(ints_hud + ints_est):
            for x, y, depois_de in partir(a, b):
                if depois_de == "queda":
                    continue
                if depois_de == "fuga":
                    x += 3.0
                if y - x >= 1.0:
                    brutos.append((x, y))
        juntos = []
        for a, b in sorted(brutos):
            separados = any(juntos and juntos[-1][1] - 0.5 <= t <= a + 0.5 for t, _ in cortes) if juntos else True
            if juntos and not separados and a <= juntos[-1][1] + gap:
                juntos[-1][1] = max(juntos[-1][1], b)
            else:
                juntos.append([a, b])
        return [(a, b) for a, b in juntos if b - a >= minimo]

    def _confirmar_papel(self, eventos):
        res = [e for e in eventos if e["tipo"] == "MATCH_RESULT"]
        if res and any(e["papel"] != self.papel for e in res):
            log.warning("Tela de resultado indica %s, mas o feed foi lido como %s.", res[0]["papel"], self.papel)
            self.papel_conflito = res[0]["papel"]

    @staticmethod
    def _chases_assassino(eventos, maximo=150.0, sem_inicio=25.0):
        ev, aberta, ultimo_fim = [], None, -1e9
        for e in sorted(eventos, key=lambda x: (x["t"], 0 if x["tipo"] == "K_CHASE_POPUP" else 1)):
            if e["tipo"] == "K_CHASE_POPUP":
                if aberta is not None and e["t"] - aberta <= maximo:
                    ini = aberta
                else:
                    ini = max(ultimo_fim + 1.0, e["t"] - sem_inicio)
                if e["t"] - ini >= 2.0:
                    ev += [{"t": round(ini, 2), "tipo": "CHASE_START", "fonte": "feed"},
                           {"t": round(e["t"], 2), "tipo": "CHASE_END", "fonte": "feed"}]
                aberta, ultimo_fim = None, e["t"]
            elif e["tipo"] == "K_FOUND":
                if aberta is None or e["t"] - aberta > maximo:
                    aberta = e["t"]
        ints = []
        for a, b in zip(ev[::2], ev[1::2]):
            if ints and a["t"] - ints[-1][1] <= 5.0:
                ints[-1][1] = b["t"]
            else:
                ints.append([a["t"], b["t"]])
        ev = [x for a, b in ints for x in ({"t": round(a, 2), "tipo": "CHASE_START", "fonte": "feed"},
                                           {"t": round(b, 2), "tipo": "CHASE_END", "fonte": "feed"})]
        total = sum(b - a for a, b in ints)
        return ev, {"chase_pov_s": round(total, 1), "chase_final_s": round(total, 1)}

    def _chases_por_colchete(self, amostras, eventos, gap=4.0, minimo=3.0):
        dt = 1.0 / self.fps_amostra
        info = {"chase_pov_s": 0.0, "linhas_pov": []}
        if not amostras:
            return [], info
        perseg = []
        for t, n in amostras:
            o = sorted(range(len(n)), key=lambda i: -n[i])
            a, b = n[o[0]], n[o[1]]
            perseg.append((t, o[0] if a >= LIMIAR_CHASE and a >= 1.6 * max(b, 0.02) else None))
        banners = sorted(e["t"] for e in eventos if e["tipo"] == "MATCH_BANNER")
        limites = [-1.0] + banners + [1e12]
        gatilhos = [e["t"] for e in eventos if e["tipo"] in BOLDNESS or e["tipo"] == "CHASE_ESCAPED"]
        faixas = []
        for a, b in zip(limites, limites[1:]):
            votos = [0, 0, 0, 0]
            for gt in gatilhos:
                if a <= gt < b:
                    for t, r in perseg:
                        if r is not None and abs(t - gt) <= 2.0:
                            votos[r] += 1
            linha = int(np.argmax(votos)) if max(votos) >= 2 else 0
            faixas.append((a, b, linha))
            info["linhas_pov"].append({"de": a, "ate": b, "linha": linha, "votos": votos})

        def linha_de(t, janela=120.0, _cache={}):
            chave = int(t // 10)
            if chave in _cache:
                return _cache[chave]
            votos = [0, 0, 0, 0]
            for gt in gatilhos:
                if abs(gt - t) <= janela:
                    for tt, r in perseg:
                        if r is not None and abs(tt - gt) <= 2.0:
                            votos[r] += 1
            if max(votos) >= 2:
                linha = int(np.argmax(votos))
            else:
                linha = next((l for a, b, l in faixas if a <= t < b), 0)
            _cache[chave] = linha
            return linha

        def intervalos(serie):
            out, ini, ult = [], None, None
            for t, on in serie:
                if on:
                    if ini is None:
                        ini = t
                    elif t - ult > gap:
                        out.append((ini, ult + dt / 2))
                        ini = t
                    ult = t
            if ini is not None:
                out.append((ini, ult + dt / 2))
            return [(round(a, 2), round(b, 2)) for a, b in out if b - a >= minimo]

        pov = intervalos([(t, r is not None and r == linha_de(t)) for t, r in perseg])
        outros = intervalos([(t, r is not None and r != linha_de(t)) for t, r in perseg])
        ev = []
        for a, b in pov:
            ev += [{"t": a, "tipo": "CHASE_START", "fonte": "hud"}, {"t": b, "tipo": "CHASE_END", "fonte": "hud"}]
        for a, b in outros:
            ev += [{"t": a, "tipo": "TEAM_CHASE_START"}, {"t": b, "tipo": "TEAM_CHASE_END"}]
        quedas = [e["t"] for e in eventos if e["tipo"] == "DOWNED"]
        for a, b in pov:
            if any(b - 2 <= q <= b + 4 for q in quedas):
                continue
            if any(b - 1 <= oa <= b + 6 for oa, ob in outros):
                ev.append({"t": b, "tipo": "SEDEX"})
        info["chase_pov_s"] = round(sum(b - a for a, b in pov), 1)
        return ev, info

    @staticmethod
    def _dedupe_fronteiras(ev):
        out, ultimo = [], {}
        pontuais = ({t for v in MAPA_SCORE.values() for t in v} | {t for v in MAPA_KILLER.values() for t in v}
                    | {"_BOLD", "MATCH_BANNER", "DOWNED", "HIT_RECEIVED", "MATCH_RESULT", "PLACAR"})
        janela = {"MATCH_BANNER": 15.0, "DOWNED": 20.0, "HIT_RECEIVED": 20.0, "MATCH_RESULT": 60.0, "PLACAR": 60.0}
        gens = None
        for e in ev:
            tp = e["tipo"]
            if tp == "GENS_RESTANTES":
                if e["valor"] == gens:
                    continue
                gens = e["valor"]
            elif tp in pontuais:
                if e["t"] - ultimo.get(tp, -99) <= janela.get(tp, 3.0) and tp not in ("UNHOOK",):
                    if tp != "_BOLD":
                        continue
                ultimo[tp] = e["t"]
            out.append(e)
        return out

    @staticmethod
    def _unhook_unico(ev, janela=25.0):
        out, vistos = [], []
        for e in ev:
            if e["tipo"] == "UNHOOK":
                if any(abs(e["t"] - v) <= janela for v in vistos):
                    continue
                vistos.append(e["t"])
            out.append(e)
        return out

    @staticmethod
    def _stun_implica_drop(ev, janela=3.0):
        drops = [e for e in ev if e["tipo"] == "PALLET_DROP"]
        carregado = [e["t"] for e in ev if e["tipo"] in ("CARRIED_END", "WIGGLE_GREAT", "WIGGLE_GOOD", "WIGGLE_POPUP")]
        extra = []
        for e in ev:
            if e["tipo"] != "PALLET_STUN":
                continue
            perto = [d for d in drops if abs(d["t"] - e["t"]) <= janela]
            if not perto and any(0 <= e["t"] - c <= 10 for c in carregado):
                e["tipo"] = "DS_STUN"
                continue
            if perto:
                min(perto, key=lambda d: abs(d["t"] - e["t"]))["t"] = round(e["t"] - 0.2, 2)
            else:
                extra.append({"t": round(e["t"] - 0.2, 2), "tipo": "PALLET_DROP", "inferido": True})
        return sorted(ev + extra, key=lambda e: e["t"])

    @staticmethod
    def _inferir_chases(ev, duracao, margem=5.0, gap_max=15.0):
        out, aberta, ultimo_bold, ultimo_fim = [], None, None, -99.0
        for e in ev:
            tipo = e["tipo"]
            bold = tipo in BOLDNESS or tipo == "_BOLD"
            if tipo == "_BOLD" and e["t"] - ultimo_fim < 6:
                bold = False
            if tipo == "MATCH_BANNER" and aberta is not None:
                out.append({"t": e["t"], "tipo": "CHASE_END", "inferido": True})
                aberta, ultimo_bold, ultimo_fim = None, None, e["t"]
            if bold:
                if aberta is None:
                    aberta = max(0.0, ultimo_fim + 0.5, e["t"] - margem)
                    out.append({"t": round(aberta, 2), "tipo": "CHASE_START", "inferido": True})
                elif ultimo_bold is not None and e["t"] - ultimo_bold > gap_max:
                    fim = ultimo_bold + margem
                    out.append({"t": round(fim, 2), "tipo": "CHASE_END", "inferido": True})
                    aberta = max(fim + 0.5, e["t"] - margem)
                    out.append({"t": round(aberta, 2), "tipo": "CHASE_START", "inferido": True})
                ultimo_bold = e["t"]
            if tipo == "CHASE_ESCAPED" or (tipo == "DOWNED" and aberta is not None):
                if aberta is None:
                    out.append({"t": round(max(0.0, ultimo_fim + 0.5, e["t"] - 30), 2),
                                "tipo": "CHASE_START", "inferido": True})
                out.append({"t": e["t"], "tipo": "CHASE_END", "inferido": True,
                            "motivo": "caiu" if tipo == "DOWNED" else "escapou"})
                aberta, ultimo_bold, ultimo_fim = None, None, e["t"]
            if tipo != "_BOLD":
                out.append(e)
        if aberta is not None:
            out.append({"t": round(min(duracao, (ultimo_bold or aberta) + margem), 2),
                        "tipo": "CHASE_END", "inferido": True})
        return sorted(out, key=lambda e: e["t"])


def _trabalho_trecho(caminho, inicio, duracao, escala, fps, limiar, tesseract_cmd, papel="sobrevivente",
                     idioma="en"):
    cv2.setNumThreads(1)
    d = DetectorHUD(fps_amostra=fps, limiar=limiar, tesseract_cmd=tesseract_cmd)
    d.definir_conjunto(papel, idioma)
    d._preparar(escala)
    return d.analisar_trecho(caminho, inicio, duracao)


def dividir_partidas(eventos, duracao_total, min_duracao=60.0, pos_fuga=45.0, pos_resultado=40.0):
    cortes, ultimo_gens, ultima_fuga = [], None, None
    leituras_gens = [(e["t"], e["valor"]) for e in eventos if e["tipo"] == "GENS_RESTANTES"]
    for e in eventos:
        if e["tipo"] == "MATCH_BANNER":
            prox = next((v for t, v in leituras_gens if e["t"] < t <= e["t"] + 120), None)
            if prox in (None, 5):
                cortes.append((e["t"] - 1, "banner", e.get("mapa")))
        elif e["tipo"] == "MATCH_RESULT":
            cortes.append((e["t"] + pos_resultado, "resultado", None))
            ultima_fuga = None
        elif e["tipo"] == "GENS_RESTANTES":
            if ultimo_gens is not None and e["valor"] == 5 and ultimo_gens <= 3:
                cortes.append((e["t"] - 1, "gens", None))
            ultimo_gens = e["valor"]
        elif e["tipo"] == "ESCAPE":
            ultima_fuga = e["t"]
        elif (ultima_fuga is not None and e["tipo"] not in ("CHASE_END", "MATCH_END", "_BOLD", "PLACAR")
              and e["t"] - ultima_fuga > pos_fuga):
            cortes.append((ultima_fuga + 10, "fuga", None))
            ultima_fuga = None
    limpos = []
    for c in sorted(cortes):
        if limpos and c[0] - limpos[-1][0] < 90:
            if c[1] == "banner" and limpos[-1][1] != "resultado":
                limpos[-1] = c
            elif c[1] == "banner":
                limpos.append(c)
            continue
        limpos.append(c)
    bordas = [(0.0, "inicio_video", None)] + [c for c in limpos if c[0] > 1] + [(duracao_total + 0.01, "fim", None)]
    familias = sorted({e["tipo"][:-6] for e in eventos if e["tipo"].endswith("_START")})
    ints = {f: _intervalos(eventos, f + "_START", f + "_END") for f in familias}
    pontas = {f + "_START" for f in familias} | {f + "_END" for f in familias}
    partidas = []
    for (a, motivo, mapa), (b, _, _) in zip(bordas, bordas[1:]):
        evs = [dict(e) for e in eventos if a <= e["t"] < b and e["tipo"] != "MATCH_BANNER" and e["tipo"] not in pontas]
        for f, lst in ints.items():
            for x, y in lst:
                x2, y2 = max(a, x), min(b, y)
                if y2 - x2 >= 1.0:
                    ini_ev = next((e for e in eventos if e["tipo"] == f + "_START" and abs(e["t"] - x) < 0.01), {})
                    extra = {k: v for k, v in ini_ev.items() if k not in ("t", "tipo")}
                    evs += [dict({"t": round(x2, 2), "tipo": f + "_START"}, **extra),
                            dict({"t": round(y2, 2), "tipo": f + "_END"}, **extra)]
        evs.sort(key=lambda e: (e["t"], 0 if e["tipo"].endswith("_START") else 1))
        relevantes = [e for e in evs if e["tipo"] not in ("GENS_RESTANTES", "MATCH_END", "MATCH_RESULT", "PLACAR")]
        if b - a < min_duracao or len(relevantes) < 3:
            continue
        primeiro_gens = next((e["valor"] for e in evs if e["tipo"] == "GENS_RESTANTES"), None)
        resultado = next((e for e in evs if e["tipo"] == "MATCH_RESULT"), None)
        placar = next((e for e in evs if e["tipo"] == "PLACAR"), None)
        fim_jogo = min(b, duracao_total)
        if resultado:
            fim_jogo = min(fim_jogo, resultado["t"])
        ini_jogo = a if motivo == "banner" else max(a, min(e["t"] for e in relevantes) - 5.0)
        partidas.append({
            "inicio": round(ini_jogo, 2), "fim": round(min(b, duracao_total), 2),
            "duracao": round(fim_jogo - ini_jogo, 2), "mapa": mapa,
            "inicio_visto": motivo == "banner" or primeiro_gens == 5,
            "resultado_tela": resultado, "placar": placar,
            "eventos": [{"t": round(ini_jogo, 2), "tipo": "MATCH_START"}] + [e for e in evs if e["t"] >= ini_jogo - 0.01]
                       + [{"t": round(fim_jogo, 2), "tipo": "MATCH_END"}],
        })
    return partidas


def saturar(valor, meio):
    return 0.0 if valor <= 0 else 100.0 * (1.0 - math.pow(0.5, valor / meio))


def razao(a, b, padrao=0.0):
    return a / b if b else padrao


def _intervalos(ev, ini, fim):
    out, aberto = [], None
    for e in ev:
        if e["tipo"] == ini and aberto is None:
            aberto = e["t"]
        elif e["tipo"] == fim and aberto is not None:
            out.append((aberto, e["t"]))
            aberto = None
    return out


def _dentro(t, ints, folga=0.0):
    return any(a - folga <= t <= b + folga for a, b in ints)


def _uniao(ints):
    tot, fim = 0.0, None
    for a, b in sorted(ints):
        if fim is None or a > fim:
            tot += b - a
            fim = b
        elif b > fim:
            tot += b - fim
            fim = b
    return tot


def extrair_metricas(eventos):
    ev = sorted(eventos, key=lambda e: e["t"])
    conta = lambda tp: sum(1 for e in ev if e["tipo"] == tp)
    tempos = lambda tp: [e["t"] for e in ev if e["tipo"] == tp]
    t_ini = next((e["t"] for e in ev if e["tipo"] == "MATCH_START"), ev[0]["t"] if ev else 0)
    t_fim = next((e["t"] for e in ev if e["tipo"] in ("ESCAPE", "MATCH_END")), ev[-1]["t"] if ev else 0)
    vivo_s = max(6.0, t_fim - t_ini)
    min_vivo = vivo_s / 60.0

    I = {k: _intervalos(ev, k + "_START", k + "_END") for k in
         ("CHASE", "GEN", "HEAL_TEAMMATE", "SELF_HEAL", "BEING_HEALED", "DYING", "CARRIED",
          "EXIT_OPEN", "TOTEM", "UNHOOK", "MEND", "ACAO_OUTRA")}
    I = {k: [(max(a, t_ini), min(b, t_fim)) for a, b in v if min(b, t_fim) - max(a, t_ini) > 0.5]
         for k, v in I.items()}
    dur = {k: sum(b - a for a, b in v) for k, v in I.items()}
    chases = I["CHASE"]

    drops, stuns = tempos("PALLET_DROP"), tempos("PALLET_STUN")
    drops_fora = sum(1 for t in drops if not _dentro(t, chases, 2.0))
    drops_stun = sum(1 for t in drops if any(0 <= s - t <= 1.5 for s in stuns))
    drops_cedo = sum(1 for t in drops for a, b in chases if a <= t <= a + 8)
    hits = conta("HIT_RECEIVED")
    hits_chase = sum(1 for t in tempos("HIT_RECEIVED") if _dentro(t, chases, 1.0))
    ocupado = _uniao([i for v in I.values() for i in v])
    gens_vals = [v for _, v in gens_confiaveis([(e["t"], e["valor"]) for e in ev if e["tipo"] == "GENS_RESTANTES"])]
    unhooks = conta("UNHOOK")
    sc_total = conta("SKILLCHECK_GREAT") + conta("SKILLCHECK_GOOD") + conta("SKILLCHECK_FAIL")

    return {
        "duracao_s": vivo_s, "min_vivo": min_vivo,
        "n_chases": len(chases), "chase_total_s": dur["CHASE"], "chase_medio_s": razao(dur["CHASE"], len(chases)),
        "maior_chase_s": max([b - a for a, b in chases], default=0.0),
        "pallets_usados": len(drops), "pallets_fora_chase": drops_fora, "pallets_cedo": drops_cedo,
        "stuns": len(stuns), "taxa_stun": razao(drops_stun, len(drops)),
        "seg_chase_por_pallet": razao(dur["CHASE"], len(drops), padrao=dur["CHASE"]),
        "hits_total": hits, "hits_por_chase": razao(hits_chase, len(chases)),
        "quedas": conta("DOWNED"), "carregado_s": dur["CARRIED"],
        "fast_vaults": conta("VAULT_FAST"), "dead_hard": conta("DEAD_HARD_DODGE"),
        "lanterna": conta("FLASHLIGHT_BLIND"),
        "gen_s": dur["GEN"], "gen_s_por_min": dur["GEN"] / min_vivo, "gen_popups": conta("GEN_PROGRESS"),
        "great_skillchecks": conta("SKILLCHECK_GREAT"),
        "taxa_fail_skillcheck": razao(conta("SKILLCHECK_FAIL"), sc_total),
        "gens_feitos_time": (max(gens_vals) - min(gens_vals)) if gens_vals else None,
        "unhooks": unhooks, "unhooks_seguros": min(conta("UNHOOK_SAFE"), unhooks),
        "taxa_unhook_seguro": razao(min(conta("UNHOOK_SAFE"), unhooks), unhooks),
        "protection_hits": conta("PROTECTION_HIT"),
        "heal_s": dur["HEAL_TEAMMATE"], "curas_popup": conta("HEAL_POPUP"),
        "auto_cura_s": dur["SELF_HEAL"], "foi_curado_s": dur["BEING_HEALED"],
        "totens": conta("TOTEM_CLEANSE") + len(I["TOTEM"]),
        "portao_s": dur["EXIT_OPEN"],
        "ocioso_pct": max(0.0, 1.0 - ocupado / vivo_s),
        "chases_escapadas": conta("CHASE_ESCAPED"), "sedex": conta("SEDEX"),
        "time_em_chase_s": sum(b - a for a, b in _intervalos(ev, "TEAM_CHASE_START", "TEAM_CHASE_END")),
        "escapou": conta("ESCAPE") > 0,
    }


REFERENCIA = {
    "chase_medio_s": (25, 60), "seg_chase_por_pallet": (15, 35), "taxa_stun": (0.3, 0.6),
    "hits_por_chase": (0.5, 1.5), "gen_s_por_min": (8, 18), "taxa_unhook_seguro": (0.4, 0.8),
    "ocioso_pct": (0.30, 0.60),
}


def nivel(m, chave):
    lo, hi = REFERENCIA[chave]
    v = m[chave]
    return "alto" if v >= hi else "baixo" if v <= lo else "medio"


REGRAS = [
    ("chase_dependente_pallet",
     lambda m: nivel(m, "chase_medio_s") != "baixo" and m["pallets_usados"] >= 2 and nivel(m, "seg_chase_por_pallet") == "baixo",
     "Chase longa, mas sustentada por gasto de pallets ({pallets_usados} pallets, {seg_chase_por_pallet:.0f}s de chase por pallet).",
     "Rode o loop mais uma volta antes de dropar. Pallet é para quando o Killer já está colado, não para abrir distância.",
     "R", -15, "alerta"),
    ("chase_eficiente",
     lambda m: nivel(m, "chase_medio_s") == "alto" and nivel(m, "seg_chase_por_pallet") == "alto",
     "Chase longa gastando pouco recurso ({seg_chase_por_pallet:.0f}s de chase por pallet).",
     "Esse é o perfil que segura o Killer sem deixar o mapa morto pro time.", "C", +10, "elogio"),
    ("pallet_precoce", lambda m: m["pallets_cedo"] >= 2,
     "{pallets_cedo} pallets dropados nos primeiros segundos da chase.",
     "Dropar logo no início entrega o recurso sem ganhar tempo. Use janela e o loop primeiro.", "R", -10, "alerta"),
    ("pallet_fora_de_chase", lambda m: m["pallets_fora_chase"] >= 1,
     "{pallets_fora_chase} pallet(s) dropado(s) sem chase ativa.",
     "Pallet sem Killer por perto é recurso perdido pro time inteiro.", "R", -12, "alerta"),
    ("pallet_sem_stun", lambda m: m["pallets_usados"] >= 3 and nivel(m, "taxa_stun") == "baixo",
     "Muitos pallets e poucos stuns ({stuns} de {pallets_usados}).",
     "Se não vai dar stun, drope só quando o Killer estiver prestes a acertar.", "M", -5, "alerta"),
    ("stun_cirurgico", lambda m: m["pallets_usados"] >= 2 and m["taxa_stun"] >= 0.6,
     "Pallets no tempo certo: {stuns} stun(s) em {pallets_usados} drops.",
     "Timing de pallet afiado; continue punindo o Killer que encosta demais.", "R", +6, "elogio"),
    ("caiu_rapido", lambda m: m["quedas"] >= 1 and m["n_chases"] >= 1 and nivel(m, "chase_medio_s") == "baixo",
     "Caiu com chases curtas ({chase_medio_s:.0f}s de média).",
     "Procure um tile forte antes do Killer chegar e evite campo aberto.", "C", -10, "alerta"),
    ("caiu_muito", lambda m: m["quedas"] >= 3,
     "Caiu {quedas} vezes na partida.",
     "Reveja a rota de fuga: guarde o recurso forte para o segundo estado de vida.", "C", -8, "alerta"),
    ("gen_baixo_sem_desculpa",
     lambda m: m["min_vivo"] >= 4 and nivel(m, "gen_s_por_min") == "baixo" and m["chase_total_s"] < 60 and m["unhooks"] == 0,
     "Pouco tempo em gerador ({gen_s:.0f}s) sem estar em chase nem salvando.",
     "Se o Killer não está em você, alguém precisa estar no gen.", "D", -15, "alerta"),
    ("looper_cumpriu_funcao", lambda m: m["chase_total_s"] >= 120,
     "Segurou o Killer por {chase_total_s:.0f}s no total — tempo que o time ganhou em gerador.",
     "Função de looper cumprida.", "D", +8, "elogio"),
    ("ocioso", lambda m: m["quedas"] == 0 and m["min_vivo"] >= 4 and nivel(m, "ocioso_pct") == "alto",
     "{ocioso_pct:.0%} do tempo sem objetivo, chase ou cura na tela.",
     "Tempo parado é derrota silenciosa. Tenha sempre um próximo alvo.", "D", -10, "alerta"),
    ("unhook_inseguro", lambda m: m["unhooks"] >= 2 and nivel(m, "taxa_unhook_seguro") == "baixo",
     "Resgates majoritariamente inseguros ({unhooks_seguros} de {unhooks} seguros).",
     "Espere o Killer se afastar ou tome o hit de proteção depois de tirar.", "T", -10, "alerta"),
    ("salvador", lambda m: m["unhooks_seguros"] >= 1 or m["protection_hits"] >= 1,
     "Jogou pelo time: {unhooks_seguros} resgate(s) seguro(s), {protection_hits} hit(s) de proteção.",
     "Altruísmo no tempo certo.", "T", +10, "elogio"),
    ("sacrificio_estrategico", lambda m: not m["escapou"] and (m["protection_hits"] >= 1 or m["chase_total_s"] >= 120),
     "Não escapou, mas segurou o Killer {chase_total_s:.0f}s / {protection_hits} hit(s) de proteção.",
     "Morte por jogada de time — o resultado pesa menos no MMR desta partida.", "S", +20, "elogio"),
    ("sedex", lambda m: m["sedex"] >= 2,
     "Em {sedex} chases o Killer foi direto para um colega logo depois que te largou — possível 'sedex'.",
     "Quando perder o Killer, não corra para onde o time está fazendo gen.", "D", -8, "alerta"),
    ("escapou_chases", lambda m: m["chases_escapadas"] >= 2,
     "Despistou o Killer {chases_escapadas} vezes (ESCAPED na HUD).",
     "Quebrar a linha de visão na hora certa é o que separa um bom looper.", "C", +5, "elogio"),
    ("dead_hard_preciso", lambda m: m["dead_hard"] >= 1,
     "{dead_hard} Dead Hard(s) desviando de hit.", "Ótima leitura do swing do Killer.", "M", +6, "elogio"),
    ("lanterna", lambda m: m["lanterna"] >= 1,
     "{lanterna} cegada(s) de lanterna no Killer.", "Lanterna bem usada salva o time.", "T", +6, "elogio"),
    ("fast_vault", lambda m: m["fast_vaults"] >= 3 and m["fast_vaults"] >= 1.5 * max(1, m["n_chases"]),
     "{fast_vaults} fast vaults: janelas bem usadas.", "Janela é recurso infinito; continue priorizando.", "M", +6, "elogio"),
    ("gerador", lambda m: m["gen_s"] >= 80 or m["great_skillchecks"] >= 3,
     "{gen_s:.0f}s em gerador e {great_skillchecks} Great skill check(s).", "Objetivo em dia.", "O", +6, "elogio"),
    ("skillcheck_ruim", lambda m: m["taxa_fail_skillcheck"] >= 0.15,
     "{taxa_fail_skillcheck:.0%} de skill checks falhos.", "Cada falha é regressão + barulho.", "M", -8, "alerta"),
]

PESOS = {"C": 0.25, "O": 0.25, "T": 0.15, "R": 0.10, "D": 0.10, "M": 0.05, "S": 0.10}
NOMES_CAT = {"C": "Chase", "O": "Objetivo", "T": "Equipe", "R": "Recursos", "D": "Decisão",
             "M": "Mecânica", "S": "Sobrevivência"}


def pesos_por_funcao(m):
    frac = min(0.8, m["chase_total_s"] / max(1.0, m["duracao_s"]))
    shift = 0.30 * frac / 0.8
    w = dict(PESOS)
    w["C"] += shift
    w["O"] -= 0.65 * shift
    w["T"] -= 0.35 * shift
    return w


def scores_base(m):
    return {
        "C": saturar(m["chase_total_s"], 60) * (1 - min(0.5, m["hits_por_chase"] * 0.2)),
        "O": min(100.0, saturar(m["gen_s"], 80) + min(15, m["totens"] * 5) + min(10, m["great_skillchecks"] * 2)),
        "T": saturar(m["unhooks"] * 30 + m["heal_s"] + m["curas_popup"] * 10
                     + m["protection_hits"] * 25 + m["lanterna"] * 15, 60),
        "R": 100 * (0.5 + 0.5 * min(1.0, m["seg_chase_por_pallet"] / 35)) if m["pallets_usados"] else 85.0,
        "D": 100 * (1 - m["ocioso_pct"] * (1.0 if m["quedas"] == 0 else 0.5)),
        "M": min(100.0, 50 + min(30, m["fast_vaults"] * 6) + min(20, m["dead_hard"] * 10)
                 - min(20, m["taxa_fail_skillcheck"] * 100)),
        "S": 100.0 if m["escapou"] else 25.0,
    }


def gens_confiaveis(leituras):
    mantidas = []
    for t, v in leituras:
        if not mantidas or v <= mantidas[-1][1]:
            mantidas.append((t, v))
    descartadas = len(leituras) - len(mantidas)
    if descartadas == 0:
        return leituras
    return mantidas if descartadas <= 1 and len(mantidas) >= 3 else []


def info_fim(partida, papel):
    partida = partida or {}
    res = partida.get("resultado_tela") or {}
    linhas = (partida.get("placar") or {}).get("linhas") or []
    sobrev = linhas[:4] if len(linhas) >= 4 else []
    out = {"texto": res.get("texto"), "escapou": None, "kills": None, "bots": 0, "personalizada": False,
           "placar": bool(linhas)}
    if sobrev:
        if papel == "assassino":
            out["bots"] = sum(1 for l in sobrev if l["bot"])
            out["personalizada"] = out["bots"] >= 4
            if all(l["status"] in ("escapou", "morto") for l in sobrev):
                out["kills"] = sum(1 for l in sobrev if l["status"] == "morto")
        else:
            outros = linhas[1:]
            out["bots"] = sum(1 for l in outros if l["bot"])
            out["personalizada"] = len(outros) >= 4 and all(l["bot"] for l in outros)
            if sobrev[0]["status"] in ("escapou", "morto"):
                out["escapou"] = sobrev[0]["status"] == "escapou"
    if res.get("papel") == "assassino" and res.get("kills") is not None:
        if res["kills"] >= 2 or out["kills"] is None:
            out["kills"] = res["kills"]
        else:
            out["kills"] = min(1, out["kills"])
    out["kills_placar"] = out["kills"]
    if res.get("papel") == "sobrevivente" and res.get("resultado") in ("escapou", "sacrificado", "morto", "desconectou"):
        out["escapou"] = res["resultado"] == "escapou"
        out["resultado_sobrevivente"] = res["resultado"]
    return out


def analisar_coach(eventos, papel="sobrevivente", partida=None):
    fim = info_fim(partida, papel)
    if papel == "assassino":
        return analisar_coach_assassino(eventos, fim)
    m = extrair_metricas(eventos)
    if fim["escapou"] is not None:
        m["escapou"] = fim["escapou"]
    m["resultado_oficial"] = fim.get("resultado_sobrevivente") or ("escapou" if m["escapou"] else None)
    m["bots"], m["personalizada"] = fim["bots"], fim["personalizada"]
    sc = scores_base(m)
    achados = []
    for rid, cond, diag, dica, cat, ajuste, tipo in REGRAS:
        try:
            if cond(m):
                sc[cat] += ajuste
                achados.append({"id": rid, "tipo": tipo, "categoria": cat,
                                "diagnostico": diag.format(**m), "dica": dica})
        except (KeyError, ZeroDivisionError, ValueError, TypeError):
            continue
    sc = {k: max(0.0, min(100.0, v)) for k, v in sc.items()}
    pesos = pesos_por_funcao(m)
    p = sum(sc[k] * pesos[k] for k in pesos)
    perfil = []
    if nivel(m, "chase_medio_s") == "alto" or m["chase_total_s"] >= 120:
        perfil.append("Gastador de pallets" if nivel(m, "seg_chase_por_pallet") == "baixo" and m["pallets_usados"] >= 2 else "Looper")
    if nivel(m, "gen_s_por_min") == "alto":
        perfil.append("Gen rusher")
    if m["unhooks"] + m["protection_hits"] + m["lanterna"] >= 3:
        perfil.append("Suporte")
    if m["fast_vaults"] + m["dead_hard"] * 2 >= 6:
        perfil.append("Mecânico")
    if m["quedas"] == 0 and m["min_vivo"] >= 4 and nivel(m, "ocioso_pct") == "alto":
        perfil.append("Passivo")
    return {"papel": "sobrevivente", "metricas": m, "scores": sc, "p_score": round(p, 1),
            "perfil": perfil or ["Equilibrado"], "achados": achados, "pesos": {k: round(v, 3) for k, v in pesos.items()}}


def extrair_metricas_assassino(eventos, fim=None):
    fim = fim or {}
    ev = sorted(eventos, key=lambda e: e["t"])
    conta = lambda tp: sum(1 for e in ev if e["tipo"] == tp)
    tempos = lambda tp: [e["t"] for e in ev if e["tipo"] == tp]
    t_ini = next((e["t"] for e in ev if e["tipo"] == "MATCH_START"), ev[0]["t"] if ev else 0)
    t_fim = next((e["t"] for e in ev if e["tipo"] == "MATCH_END"), ev[-1]["t"] if ev else 0)
    dur = max(6.0, t_fim - t_ini)
    chases = [(a, b) for a, b in _intervalos(ev, "CHASE_START", "CHASE_END") if b > t_ini and a < t_fim]
    linhas = [e.get("linha") for e in ev if e["tipo"] == "CHASE_START" and e.get("linha") is not None]
    ganchos_t = tempos("K_HOOK")
    novos = min(4, conta("K_HOOK_NOVO"))
    ganchos = max(len(ganchos_t), novos)
    primeiro = min(ganchos_t + tempos("K_HOOK_NOVO"), default=None)
    sacrificios, mortes = conta("K_SACRIFICE"), conta("K_KILL")
    kills_feed = min(4, sacrificios + mortes)
    kills = fim.get("kills")
    if kills is None:
        kills = kills_feed
    elif kills == 1 and fim.get("texto") in ("ENTITY DISPLEASED", "ENTIDADE DESAGRADADA"):
        kills = min(1, kills_feed)
    hits_b, hits_p = conta("K_HIT"), conta("KP_HIT")
    usos = conta("KP_USE")
    gens = gens_confiaveis([(e["t"], e["valor"]) for e in ev if e["tipo"] == "GENS_RESTANTES"])
    gens_fim = gens[-1][1] if gens else None
    gens_no_1o = None
    if gens and primeiro is not None:
        antes = [v for t, v in gens if t <= primeiro]
        gens_no_1o = antes[-1] if antes else gens[0][1]
    dur_chases = [b - a for a, b in chases]
    t_hits = tempos("K_HIT")
    hits = hits_b + sum(1 for t in tempos("KP_HIT") if not any(abs(t - h) <= 2.0 for h in t_hits))
    m = {
        "duracao_s": dur, "min_jogo": dur / 60.0,
        "kills": int(kills), "sacrificios": sacrificios, "mortes_entidade": mortes,
        "ganchos": ganchos, "sobreviventes_enganchados": novos, "segundo_estagio": conta("K_ENTITY"),
        "hits": hits, "hits_basicos": hits_b, "hits_poder": hits_p, "quedas_poder": conta("KP_DOWN"),
        "usos_poder": usos, "efeitos_poder": conta("KP_EFFECT"),
        "precisao_poder": (min(1.0, hits_p / usos) if usos >= 3 and hits_p >= 1 else None),
        "chutes_gerador": conta("K_GEN_KICK"), "destruicao": conta("K_DESTRUCTION"),
        "encontrados": conta("K_FOUND"), "sede_de_sangue": conta("K_BLOODLUST"),
        "cegado": conta("K_BLINDED"), "atordoado": conta("K_STUNNED"), "esquiva_lanterna": conta("K_LIGHT_DODGE"),
        "n_chases": len(chases), "chase_total_s": sum(dur_chases),
        "chase_medio_s": (sum(dur_chases) / len(chases)) if chases else None,
        "maior_chase_s": max(dur_chases, default=0.0),
        "chase_por_hit_s": (sum(dur_chases) / hits) if hits and chases else None,
        "sobreviventes_perseguidos": len(set(linhas)),
        "tempo_primeiro_gancho_s": (primeiro - t_ini) if primeiro is not None else None,
        "gens_restantes_fim": gens_fim,
        "gens_antes_1o_gancho": (5 - gens_no_1o) if gens_no_1o is not None else None,
        "ganchos_por_min": ganchos / max(1.0, dur / 60.0),
        "bots": fim.get("bots", 0), "personalizada": fim.get("personalizada", False),
        "ninguem_escapou": conta("K_NO_ONE_ESCAPED") > 0 or kills == 4,
    }
    m["tempo_primeiro_gancho_txt"] = _mmss(m["tempo_primeiro_gancho_s"]) if m["tempo_primeiro_gancho_s"] is not None else "—"
    return m


REGRAS_ASSASSINO = [
    ("chase_longa", lambda m: m["n_chases"] >= 3 and (m["chase_medio_s"] or 0) >= 50,
     "Chases longas ({chase_medio_s:.0f}s de média).",
     "Se a chase passa de 30–40 s e os gens estão andando, largue e vá atrás de outro sobrevivente.", "C", -10, "alerta"),
    ("chase_rapida", lambda m: m["n_chases"] >= 3 and m["chase_medio_s"] is not None and m["chase_medio_s"] <= 25,
     "Chases curtas ({chase_medio_s:.0f}s de média): derrubou rápido.",
     "Continue cortando caminho em vez de seguir o sobrevivente pelo loop.", "C", +8, "elogio"),
    ("sede_de_sangue", lambda m: m["sede_de_sangue"] >= 2,
     "Sede de Sangue ativou {sede_de_sangue} vezes (chase rodando no mesmo loop).",
     "Quebre o pallet ou mude de alvo em vez de esperar a Sede de Sangue.", "C", -8, "alerta"),
    ("primeiro_gancho_lento", lambda m: (m["tempo_primeiro_gancho_s"] or 0) >= 180,
     "Primeiro gancho só aos {tempo_primeiro_gancho_txt} de partida.",
     "O começo decide a partida: force a primeira queda no sobrevivente mais próximo.", "D", -10, "alerta"),
    ("primeiro_gancho_rapido", lambda m: m["tempo_primeiro_gancho_s"] is not None and m["tempo_primeiro_gancho_s"] <= 75,
     "Primeiro gancho aos {tempo_primeiro_gancho_txt}: começo forte.",
     "Pressão logo cedo tira o time dos geradores.", "D", +8, "elogio"),
    ("gens_cedo", lambda m: (m["gens_antes_1o_gancho"] or 0) >= 3,
     "O time fez {gens_antes_1o_gancho} gerador(es) antes do seu primeiro gancho.",
     "Patrulhe os geradores do meio e chute gen quando largar uma chase.", "P", -12, "alerta"),
    ("sem_chute", lambda m: m["chutes_gerador"] == 0 and m["duracao_s"] >= 300,
     "Nenhum gerador chutado na partida.",
     "Chutar gen ao passar por ele custa 2 s e atrasa o time inteiro.", "P", -8, "alerta"),
    ("pressao_gen", lambda m: m["chutes_gerador"] >= 5,
     "{chutes_gerador} geradores chutados.", "Pressão de gerador constante.", "P", +6, "elogio"),
    ("tunnel", lambda m: m["ganchos"] >= 4 and m["sobreviventes_enganchados"] <= 2,
     "Ganchos concentrados: {ganchos} ganchos em só {sobreviventes_enganchados} sobrevivente(s) (possível tunnel).",
     "Espalhar ganchos mantém os 4 sobreviventes ocupados com resgates e cura.", "E", -15, "alerta"),
    ("espalhou", lambda m: m["sobreviventes_enganchados"] >= 4,
     "Enganchou os 4 sobreviventes pelo menos uma vez.", "Pressão espalhada pelo mapa inteiro.", "E", +8, "elogio"),
    ("poder_preciso", lambda m: m["precisao_poder"] is not None and m["precisao_poder"] >= 0.5 and m["usos_poder"] >= 4,
     "{hits_poder} acerto(s) de poder em {usos_poder} usos.", "Poder bem usado.", "W", +10, "elogio"),
    ("poder_impreciso", lambda m: m["precisao_poder"] is not None and m["precisao_poder"] < 0.10 and m["usos_poder"] >= 8,
     "Poder pouco certeiro: {hits_poder} acerto(s) em {usos_poder} usos.",
     "Use o poder quando o sobrevivente não tem onde se esconder (campo aberto, fim de loop).", "W", -10, "alerta"),
    ("cegado", lambda m: m["cegado"] + m["atordoado"] >= 3,
     "Foi cegado/atordoado {cegado}+{atordoado} vezes.",
     "Olhe para a parede ao pegar alguém do chão e respeite pallets em pé.", "D", -6, "alerta"),
    ("mapa_limpo", lambda m: m["destruicao"] >= 5,
     "Quebrou {destruicao} pallets/paredes.", "Mapa mais fraco para as próximas chases.", "C", +4, "elogio"),
    ("quatro_k", lambda m: m["kills"] >= 4, "4 kills: ninguém escapou.", "Partida dominante.", "L", +10, "elogio"),
    ("muitos_ganchos", lambda m: m["ganchos"] >= 8, "{ganchos} ganchos na partida.", "Pressão de gancho alta.", "L", +5,
     "elogio"),
]
PESOS_ASSASSINO = {"L": 0.25, "P": 0.20, "C": 0.20, "E": 0.10, "W": 0.10, "D": 0.15}
NOMES_CAT_ASSASSINO = {"L": "Letalidade", "P": "Pressão", "C": "Chase", "E": "Espalhamento", "W": "Poder",
                       "D": "Decisão"}


def _linear(v, bom, ruim):
    if v is None:
        return None
    if bom < ruim:
        return 100.0 if v <= bom else 25.0 if v >= ruim else 100.0 - 75.0 * (v - bom) / (ruim - bom)
    return 100.0 if v >= bom else 25.0 if v <= ruim else 25.0 + 75.0 * (v - ruim) / (bom - ruim)


def scores_assassino(m):
    gens = 50.0 if m["gens_restantes_fim"] is None else 100.0 * m["gens_restantes_fim"] / 5.0
    chase = _linear(m["chase_medio_s"], 20, 75)
    if chase is None:
        chase = 60.0 if m["hits"] else 50.0
    if m["precisao_poder"] is not None:
        poder = 40.0 + 60.0 * m["precisao_poder"]
    else:
        poder = 70.0 if m["hits_poder"] else 45.0 if m["usos_poder"] else 50.0
    decisao = _linear(m["tempo_primeiro_gancho_s"], 75, 240)
    if decisao is None:
        decisao = 30.0 if m["duracao_s"] > 240 else 50.0
    return {
        "L": min(100.0, m["kills"] * 20.0 + min(m["ganchos"], 12) * 1.7),
        "P": 0.55 * gens + 0.25 * saturar(m["chutes_gerador"], 3) + 0.20 * saturar(m["ganchos_por_min"], 0.5),
        "C": chase - min(15, m["sede_de_sangue"] * 5),
        "E": (100.0 * m["sobreviventes_enganchados"] / 4.0) if m["ganchos"] >= 2 else 50.0,
        "W": poder,
        "D": decisao - min(20, (m["cegado"] + m["atordoado"]) * 5),
    }


def analisar_coach_assassino(eventos, fim):
    m = extrair_metricas_assassino(eventos, fim)
    sc = scores_assassino(m)
    achados = []
    for rid, cond, diag, dica, cat, ajuste, tipo in REGRAS_ASSASSINO:
        try:
            if cond(m):
                sc[cat] += ajuste
                achados.append({"id": rid, "tipo": tipo, "categoria": cat,
                                "diagnostico": diag.format(**m), "dica": dica})
        except (KeyError, ZeroDivisionError, ValueError, TypeError):
            continue
    sc = {k: max(0.0, min(100.0, v)) for k, v in sc.items()}
    p = sum(sc[k] * PESOS_ASSASSINO[k] for k in PESOS_ASSASSINO)
    ids = {a["id"] for a in achados}
    perfil = []
    if "chase_rapida" in ids:
        perfil.append("Caçador")
    if m["chutes_gerador"] >= 5 or "pressao_gen" in ids:
        perfil.append("Pressão de gens")
    if "espalhou" in ids:
        perfil.append("Espalha ganchos")
    if "tunnel" in ids:
        perfil.append("Tunnel")
    if "poder_preciso" in ids:
        perfil.append("Mestre do poder")
    return {"papel": "assassino", "metricas": m, "scores": sc, "p_score": round(p, 1),
            "perfil": perfil or ["Equilibrado"], "achados": achados, "pesos": dict(PESOS_ASSASSINO)}


TIERS = [(1800, "Iridescente", 0xB388FF), (1600, "Ouro", 0xF1C40F), (1450, "Prata", 0xBDC3C7),
         (1300, "Bronze", 0xCD7F32), (-1e9, "Cinzas", 0x7F8C8D)]


def tier(mmr):
    return next((n, c) for lim, n, c in TIERS if mmr >= lim)


def calcular_mmr(mmr, rd, dias_inativo, mmr_ref, p_score, resultado_s, peso_res, confianca, completa, cfg,
                 fator_c=1.0, vale_mmr=True):
    RD_MIN, RD_MAX = 50.0, 350.0
    c_inat = math.sqrt((RD_MAX ** 2 - RD_MIN ** 2) / 100.0)
    rd_ini = min(RD_MAX, math.sqrt(rd ** 2 + c_inat ** 2 * max(0.0, dias_inativo)))
    E = 1.0 / (1.0 + 10 ** ((mmr_ref - mmr) / 400.0))
    U = 0.5 + (max(RD_MIN, min(RD_MAX, rd_ini)) - RD_MIN) / (RD_MAX - RD_MIN)
    S = peso_res * resultado_s + (1 - peso_res) * (p_score / 100.0)
    C = max(0.3, min(1.0, confianca / 0.8)) * (1.0 if completa else 0.5) * fator_c
    delta = cfg.k * (S - E) * U * C if vale_mmr else 0.0
    delta = max(-cfg.delta_max, min(cfg.delta_max, delta))
    return {"mmr_anterior": mmr, "mmr_novo": round(mmr + delta, 1), "delta": round(delta, 1),
            "rd_anterior": rd, "rd_novo": round(max(RD_MIN, rd_ini * 0.95), 1) if vale_mmr else rd,
            "E": round(E, 3), "U": round(U, 3), "S_comb": round(S, 3), "C_match": round(C, 3),
            "mmr_ref": round(mmr_ref, 1), "vale_mmr": vale_mmr}


def parametros_mmr(coach):
    m = coach["metricas"]
    vale = not m.get("personalizada")
    if coach["papel"] == "assassino":
        fator = 0.6 if m.get("bots") else 1.0
        return m["kills"] / 4.0, 0.35, fator, vale
    sacrificio = any(a["id"] == "sacrificio_estrategico" for a in coach["achados"])
    peso = 0.10 if (sacrificio and not m["escapou"]) else 0.30
    fator = 0.8 if m.get("bots") else 1.0
    return (1.0 if m["escapou"] else 0.0), peso, fator, vale


try:
    import psycopg2 as _pg
except ImportError:
    try:
        import psycopg as _pg
    except ImportError:
        _pg = None

SQL_MIGRACAO = [
    """CREATE TABLE IF NOT EXISTS jogadores (
        id_discord VARCHAR(50) PRIMARY KEY,
        mmr FLOAT DEFAULT 1500.0,
        rd FLOAT DEFAULT 350.0,
        partidas_jogadas INTEGER DEFAULT 0,
        ultima_atualizacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
    """ALTER TABLE jogadores
        ADD COLUMN IF NOT EXISTS nome_discord VARCHAR(100),
        ADD COLUMN IF NOT EXISTS fugas INTEGER DEFAULT 0,
        ADD COLUMN IF NOT EXISTS mmr_pico FLOAT""",
    """CREATE TABLE IF NOT EXISTS historico_partidas (
        match_id SERIAL PRIMARY KEY,
        id_discord VARCHAR(50) REFERENCES jogadores(id_discord),
        mmr_anterior FLOAT, mmr_novo FLOAT, delta_mmr FLOAT,
        performance_score FLOAT, resultado_combinado FLOAT, confianca_partida FLOAT,
        data_partida TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
    """ALTER TABLE historico_partidas
        ADD COLUMN IF NOT EXISTS rd_anterior FLOAT,
        ADD COLUMN IF NOT EXISTS rd_novo FLOAT,
        ADD COLUMN IF NOT EXISTS video_key TEXT,
        ADD COLUMN IF NOT EXISTS video_sha256 VARCHAR(64),
        ADD COLUMN IF NOT EXISTS indice_partida INTEGER DEFAULT 0,
        ADD COLUMN IF NOT EXISTS total_partidas_video INTEGER DEFAULT 1,
        ADD COLUMN IF NOT EXISTS resultado VARCHAR(20),
        ADD COLUMN IF NOT EXISTS mapa TEXT,
        ADD COLUMN IF NOT EXISTS perfil TEXT,
        ADD COLUMN IF NOT EXISTS duracao_s FLOAT,
        ADD COLUMN IF NOT EXISTS partida_completa BOOLEAN,
        ADD COLUMN IF NOT EXISTS escala_hud FLOAT,
        ADD COLUMN IF NOT EXISTS scores JSONB,
        ADD COLUMN IF NOT EXISTS metricas JSONB,
        ADD COLUMN IF NOT EXISTS achados JSONB,
        ADD COLUMN IF NOT EXISTS calculo_mmr JSONB,
        ADD COLUMN IF NOT EXISTS versao_motor VARCHAR(20)""",
    """ALTER TABLE jogadores
        ADD COLUMN IF NOT EXISTS mmr_assassino FLOAT DEFAULT 1500.0,
        ADD COLUMN IF NOT EXISTS rd_assassino FLOAT DEFAULT 350.0,
        ADD COLUMN IF NOT EXISTS partidas_assassino INTEGER DEFAULT 0,
        ADD COLUMN IF NOT EXISTS mmr_pico_assassino FLOAT,
        ADD COLUMN IF NOT EXISTS kills_total INTEGER DEFAULT 0,
        ADD COLUMN IF NOT EXISTS ultima_assassino TIMESTAMP""",
    """ALTER TABLE historico_partidas
        ADD COLUMN IF NOT EXISTS papel VARCHAR(20) DEFAULT 'sobrevivente',
        ADD COLUMN IF NOT EXISTS idioma VARCHAR(5),
        ADD COLUMN IF NOT EXISTS kills INTEGER,
        ADD COLUMN IF NOT EXISTS bots INTEGER DEFAULT 0,
        ADD COLUMN IF NOT EXISTS personalizada BOOLEAN DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS resultado_tela TEXT""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_hist_video_partida
        ON historico_partidas (video_sha256, indice_partida) WHERE video_sha256 IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS ix_hist_jogador_data ON historico_partidas (id_discord, data_partida DESC)""",
]


COLUNAS_PAPEL = {
    "sobrevivente": {"mmr": "mmr", "rd": "rd", "n": "partidas_jogadas", "pico": "mmr_pico", "ult": "ultima_atualizacao"},
    "assassino": {"mmr": "mmr_assassino", "rd": "rd_assassino", "n": "partidas_assassino", "pico": "mmr_pico_assassino",
                  "ult": "ultima_assassino"},
}

class Banco:
    def __init__(self, url):
        if _pg is None:
            raise RuntimeError("Instale o driver do Postgres: pip install psycopg2-binary")
        self.url = url

    def _con(self):
        return _pg.connect(self.url, connect_timeout=15)

    def _exec(self, sql, params=(), um=False, todos=False):
        conn = self._con()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            r = cur.fetchone() if um else cur.fetchall() if todos else None
            conn.commit()
            return r
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def preparar(self):
        conn = self._con()
        try:
            cur = conn.cursor()
            for sql in SQL_MIGRACAO:
                cur.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        log.info("Banco OK (tabelas jogadores / historico_partidas verificadas).")

    def partidas_ja_gravadas(self, sha):
        rows = self._exec("SELECT indice_partida, total_partidas_video FROM historico_partidas WHERE video_sha256 = %s",
                          (sha,), todos=True) or []
        feitos = {r[0] for r in rows}
        total = max((r[1] or 1 for r in rows), default=None)
        return feitos, total

    def partidas_24h(self, jogador_id):
        r = self._exec("""SELECT COUNT(*) FROM historico_partidas
                          WHERE id_discord = %s AND data_partida > CURRENT_TIMESTAMP - INTERVAL '24 hours'""",
                       (jogador_id,), um=True)
        return int(r[0]) if r else 0

    def mmr_referencia(self, padrao, papel="sobrevivente"):
        c = COLUNAS_PAPEL[papel]
        r = self._exec(f"SELECT AVG({c['mmr']}), COUNT(*) FROM jogadores WHERE {c['n']} >= 5", um=True)
        return float(r[0]) if r and r[0] is not None and r[1] >= 3 else padrao

    def ranking(self, jogador_id, papel="sobrevivente"):
        c = COLUNAS_PAPEL[papel]
        r = self._exec(f"""SELECT (SELECT COUNT(*) + 1 FROM jogadores j2 WHERE j2.{c['n']} > 0 AND j2.{c['mmr']} > j.{c['mmr']}),
                                  (SELECT COUNT(*) FROM jogadores WHERE {c['n']} > 0)
                           FROM jogadores j WHERE j.id_discord = %s AND j.{c['n']} > 0""", (jogador_id,), um=True)
        return (int(r[0]), int(r[1])) if r else (None, None)

    def registrar_partida(self, jogador_id, nome, dados, calc, cfg):
        papel = dados.get("papel", "sobrevivente")
        c = COLUNAS_PAPEL[papel]
        conn = self._con()
        try:
            cur = conn.cursor()
            cur.execute("""INSERT INTO jogadores (id_discord, mmr, rd, mmr_pico) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (id_discord) DO NOTHING""",
                        (jogador_id, cfg.mmr_inicial, cfg.rd_inicial, cfg.mmr_inicial))
            cur.execute(f"""SELECT {c['mmr']}, {c['rd']}, {c['n']},
                                   COALESCE(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - {c['ult']})) / 86400.0, 0)
                            FROM jogadores WHERE id_discord = %s FOR UPDATE""", (jogador_id,))
            mmr, rd, n_part, dias = cur.fetchone()
            mmr = float(mmr if mmr is not None else cfg.mmr_inicial)
            rd = float(rd if rd is not None else cfg.rd_inicial)
            n_part = int(n_part or 0)
            res = calc(mmr, rd, float(dias) if n_part else 0.0)
            escapou = dados["resultado"] == "escapou"
            if res.get("vale_mmr", True):
                cur.execute(f"""UPDATE jogadores
                               SET {c['mmr']} = %s, {c['rd']} = %s, {c['n']} = COALESCE({c['n']}, 0) + 1,
                                   fugas = COALESCE(fugas, 0) + %s, kills_total = COALESCE(kills_total, 0) + %s,
                                   {c['pico']} = GREATEST(COALESCE({c['pico']}, %s), %s),
                                   nome_discord = COALESCE(%s, nome_discord),
                                   {c['ult']} = CURRENT_TIMESTAMP
                               WHERE id_discord = %s""",
                            (res["mmr_novo"], res["rd_novo"], 1 if escapou else 0, int(dados.get("kills") or 0),
                             res["mmr_novo"], res["mmr_novo"], nome, jogador_id))
                n_part += 1
            elif nome:
                cur.execute("UPDATE jogadores SET nome_discord = %s WHERE id_discord = %s", (nome, jogador_id))
            cur.execute("""INSERT INTO historico_partidas
                (id_discord, mmr_anterior, mmr_novo, delta_mmr, performance_score, resultado_combinado,
                 confianca_partida, rd_anterior, rd_novo, video_key, video_sha256, indice_partida,
                 total_partidas_video, resultado, mapa, perfil, duracao_s, partida_completa, escala_hud,
                 scores, metricas, achados, calculo_mmr, versao_motor, papel, idioma, kills, bots, personalizada,
                 resultado_tela)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)
                RETURNING match_id""",
                        (jogador_id, res["mmr_anterior"], res["mmr_novo"], res["delta"], dados["p_score"],
                         res["S_comb"], res["C_match"], res["rd_anterior"], res["rd_novo"], dados["video_key"],
                         dados["sha"], dados["indice"], dados["total"], dados["resultado"], dados["mapa"],
                         ", ".join(dados["perfil"]), dados["duracao"], dados["completa"], dados["escala"],
                         json.dumps(dados["scores"]), json.dumps(dados["metricas"], default=float),
                         json.dumps(dados["achados"], ensure_ascii=False), json.dumps(res), VERSAO,
                         papel, dados.get("idioma"), dados.get("kills"), int(dados.get("bots") or 0),
                         bool(dados.get("personalizada")), dados.get("resultado_tela")))
            res["match_id"] = cur.fetchone()[0]
            res["partidas_jogadas"] = n_part
            conn.commit()
            return res
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


PROCESSANDO, FALHAS, META = "processando/", "falhas/", "meta/"
EXT_VIDEO = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
RE_CHAVE = re.compile(r"^(?P<id>[^/]+?)_(?P<ts>\d{13})_(?P<nome>.+)$")
RE_SNOWFLAKE = re.compile(r"^\d{17,20}$")
RE_ID = re.compile(r"^(\d{17,20}|[A-Za-z0-9][A-Za-z0-9:_\-]{5,49})$")


def ler_chave(key):
    m = RE_CHAVE.match(key)
    if not m:
        return None
    return {"jogador_id": m.group("id"), "enviado_em": int(m.group("ts")) / 1000.0, "arquivo": m.group("nome")}


class FilaR2:
    def __init__(self, cfg):
        import boto3
        from botocore.config import Config as BotoConfig
        self.bucket = cfg.r2_bucket
        self.descricao = f"bucket {cfg.r2_bucket}"
        self.s3 = boto3.client("s3", endpoint_url=cfg.r2_endpoint, aws_access_key_id=cfg.r2_key,
                               aws_secret_access_key=cfg.r2_secret, region_name="auto",
                               config=self._boto_cfg(BotoConfig))

    @staticmethod
    def _boto_cfg(BotoConfig):
        base = dict(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"},
                    connect_timeout=20, read_timeout=120)
        try:
            return BotoConfig(request_checksum_calculation="when_required",
                              response_checksum_validation="when_required", **base)
        except TypeError:
            return BotoConfig(**base)

    def _listar(self, prefixo=""):
        pag = self.s3.get_paginator("list_objects_v2")
        for pg in pag.paginate(Bucket=self.bucket, Prefix=prefixo):
            for o in pg.get("Contents", []) or []:
                yield o

    def pendentes(self):
        objs = [o for o in self._listar()
                if not o["Key"].startswith((PROCESSANDO, FALHAS, META)) and not o["Key"].endswith("/")]
        return sorted(objs, key=lambda o: o["LastModified"])

    def reivindicar(self, key):
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return None
        meta = dict(head.get("Metadata") or {})
        meta["tentativas"] = str(int(meta.get("tentativas", "0") or 0) + 1)
        destino = PROCESSANDO + key
        self._copiar(key, destino, meta, head.get("ContentType", "video/mp4"))
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            pass
        return destino, int(meta["tentativas"]), int(head.get("ContentLength", 0))

    def _copiar(self, origem, destino, meta=None, content_type="video/mp4"):
        from boto3.s3.transfer import TransferConfig
        extra = {"ContentType": content_type}
        if meta is not None:
            extra.update({"Metadata": meta, "MetadataDirective": "REPLACE"})
        self.s3.copy({"Bucket": self.bucket, "Key": origem}, self.bucket, destino, ExtraArgs=extra,
                     Config=TransferConfig(multipart_threshold=4 * 1024 ** 3, multipart_chunksize=512 * 1024 ** 2))

    def ler_meta(self, key):
        try:
            corpo = self.s3.get_object(Bucket=self.bucket, Key=f"{META}{key}.json")["Body"].read()
            dados = json.loads(corpo.decode("utf-8"))
            return dados if isinstance(dados, dict) else {}
        except Exception:
            return {}

    def apagar_meta(self, key):
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=f"{META}{key}.json")
        except Exception:
            pass

    def baixar(self, key, caminho):
        from boto3.s3.transfer import TransferConfig
        self.s3.download_file(self.bucket, key, caminho,
                              Config=TransferConfig(multipart_chunksize=64 * 1024 ** 2, max_concurrency=8))

    def mover_para_falhas(self, key_proc, motivo):
        original = key_proc[len(PROCESSANDO):] if key_proc.startswith(PROCESSANDO) else key_proc
        import unicodedata
        motivo_ascii = unicodedata.normalize("NFKD", motivo).encode("ascii", "ignore").decode()
        motivo_ascii = re.sub(r"[^A-Za-z0-9 .,:;_()\-]", "", motivo_ascii)[:500]
        try:
            self._copiar(key_proc, FALHAS + original,
                         {"motivo": motivo_ascii or "erro", "quando": datetime.now(timezone.utc).isoformat()})
            self.s3.delete_object(Bucket=self.bucket, Key=key_proc)
        except Exception as e:
            log.error("Não consegui mover %s para falhas/: %s", key_proc, e)

    def apagar(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def recuperar_orfaos(self, idade_min=90, max_tentativas=3):
        agora = datetime.now(timezone.utc)
        for o in self._listar(PROCESSANDO):
            if (agora - o["LastModified"]).total_seconds() < idade_min * 60:
                continue
            key = o["Key"]
            try:
                meta = self.s3.head_object(Bucket=self.bucket, Key=key).get("Metadata") or {}
                if int(meta.get("tentativas", "1")) >= max_tentativas:
                    log.warning("%s falhou %s vezes; indo para falhas/", key, meta.get("tentativas"))
                    self.mover_para_falhas(key, "travou o worker varias vezes")
                    continue
                self._copiar(key, key[len(PROCESSANDO):], dict(meta))
                self.s3.delete_object(Bucket=self.bucket, Key=key)
                log.info("Recolocado na fila: %s", key)
            except Exception as e:
                log.error("Falha ao recuperar %s: %s", key, e)


class FilaSite:
    def __init__(self, cfg):
        import requests
        self.base = cfg.site_url
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {cfg.worker_token}"
        self.s.headers["User-Agent"] = f"dbd-coach-worker/{VERSAO}"
        self.descricao = f"site {self.base}"

    def _pedir(self, metodo, rota, ok=(200,), **kw):
        kw.setdefault("timeout", 60)
        ultimo = None
        for tentativa in range(4):
            try:
                r = self.s.request(metodo, self.base + rota, **kw)
            except Exception as e:
                ultimo = e
                time.sleep(2 * (tentativa + 1))
                continue
            if r.status_code == 401:
                raise RuntimeError("WORKER_TOKEN recusado pelo site (precisa ser igual no worker e no index.js)")
            if r.status_code in ok or r.status_code < 500:
                return r
            ultimo = RuntimeError(f"site respondeu {r.status_code} em {rota}")
            time.sleep(2 * (tentativa + 1))
        raise ultimo

    def pendentes(self):
        r = self._pedir("GET", "/worker/fila")
        if r.status_code != 200:
            raise RuntimeError(f"site respondeu {r.status_code} em /worker/fila")
        itens = (r.json() or {}).get("itens") or []
        return [{"Key": i["key"], "Size": int(i.get("tamanho") or 0),
                 "LastModified": datetime.fromtimestamp(float(i.get("modificado") or 0) / 1000, timezone.utc)}
                for i in itens if i.get("key")]

    def reivindicar(self, key):
        r = self._pedir("POST", "/worker/reivindicar", json={"key": key})
        if r.status_code != 200:
            return None
        d = r.json()
        return d["key_proc"], int(d.get("tentativas") or 1), int(d.get("tamanho") or 0)

    def ler_meta(self, key):
        try:
            r = self._pedir("GET", "/worker/meta", params={"key": key})
            d = r.json() if r.status_code == 200 else {}
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def apagar_meta(self, key):
        try:
            self._pedir("DELETE", "/worker/meta", params={"key": key})
        except Exception:
            pass

    def baixar(self, key, caminho):
        total = None
        for tentativa in range(6):
            feito = os.path.getsize(caminho) if os.path.exists(caminho) else 0
            if total is not None and feito >= total:
                return
            cab = {"Range": f"bytes={feito}-"} if feito else {}
            try:
                with self.s.get(self.base + "/worker/video", params={"key": key}, headers=cab,
                                stream=True, timeout=(20, 300)) as r:
                    if r.status_code == 401:
                        raise RuntimeError("WORKER_TOKEN recusado pelo site")
                    if r.status_code in (400, 404):
                        raise RuntimeError(f"site respondeu {r.status_code} ao baixar {key}")
                    if r.status_code not in (200, 206):
                        raise IOError(f"site respondeu {r.status_code}")
                    if r.status_code == 200 and feito:
                        feito = 0
                    tam = int(r.headers.get("Content-Length") or 0)
                    total = feito + tam if tam else total
                    with open(caminho, "ab" if feito else "wb") as f:
                        for pedaco in r.iter_content(8 * 1024 * 1024):
                            f.write(pedaco)
                if total is None or os.path.getsize(caminho) >= total:
                    return
            except RuntimeError:
                raise
            except Exception as e:
                log.warning("Download interrompido (%s); retomando...", e)
            time.sleep(3 * (tentativa + 1))
        raise RuntimeError(f"não consegui baixar {key} do site")

    def mover_para_falhas(self, key_proc, motivo):
        try:
            self._pedir("POST", "/worker/falha", json={"key": key_proc, "motivo": str(motivo)[:500]})
        except Exception as e:
            log.error("Não consegui marcar %s como falha: %s", key_proc, e)

    def apagar(self, key):
        self._pedir("DELETE", "/worker/video", params={"key": key})

    def recuperar_orfaos(self, idade_min=90, max_tentativas=3):
        try:
            d = self._pedir("POST", "/worker/recuperar",
                            json={"idade_min": idade_min, "max_tentativas": max_tentativas}).json()
            for k in d.get("devolvidos") or []:
                log.info("Recolocado na fila: %s", k)
            for k in d.get("falhas") or []:
                log.warning("%s travou o worker várias vezes; marcado como falha", k)
        except Exception as e:
            log.error("Falha ao recuperar vídeos presos: %s", e)


def criar_fila(cfg):
    return FilaSite(cfg) if cfg.armazenamento == "site" else FilaR2(cfg)


def _req():
    import requests
    return requests


class Discord:
    def __init__(self, cfg):
        self.cfg = cfg
        self.webhook = cfg.webhook
        self._cache_usuarios = {}

    def usuario(self, jogador_id):
        if not self.cfg.discord_token or not RE_SNOWFLAKE.match(jogador_id):
            return None
        if jogador_id in self._cache_usuarios:
            return self._cache_usuarios[jogador_id]
        try:
            r = _req().get(f"https://discord.com/api/v10/users/{jogador_id}",
                           headers={"Authorization": f"Bot {self.cfg.discord_token}"}, timeout=15)
            if r.status_code == 200:
                u = r.json()
                av = u.get("avatar")
                info = {"existe": True, "nome": u.get("global_name") or u.get("username"),
                        "avatar": f"https://cdn.discordapp.com/avatars/{jogador_id}/{av}.png?size=128" if av else None}
            elif r.status_code in (404, 400):
                info = {"existe": False}
            else:
                return None
        except Exception:
            return None
        self._cache_usuarios[jogador_id] = info
        return info

    def enviar_arquivo(self, payload, caminho, nome=None, tentativas=4):
        if not self.webhook:
            return False
        req = _req()
        payload = dict(payload)
        payload.setdefault("username", "DBD Coach · " + self.cfg.nome_servidor)
        payload.setdefault("allowed_mentions", {"parse": []})
        nome = nome or os.path.basename(caminho)
        for i in range(tentativas):
            try:
                with open(caminho, "rb") as f:
                    r = req.post(self.webhook, params={"wait": "true"}, timeout=120,
                                 data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                                 files={"files[0]": (nome, f, "video/mp4")})
                if r.status_code in (200, 204):
                    return True
                if r.status_code == 429:
                    time.sleep(min(30.0, float((r.json() or {}).get("retry_after", 2))) + 0.5)
                    continue
                if r.status_code == 413:
                    log.warning("Discord recusou o clipe por tamanho (%.1f MB).", os.path.getsize(caminho) / 1e6)
                    return False
                if r.status_code >= 500:
                    time.sleep(2 * (i + 1))
                    continue
                log.error("Discord recusou o clipe (%s): %s", r.status_code, r.text[:300])
                return False
            except Exception as e:
                log.warning("Falha ao enviar clipe (%s); tentando de novo...", e)
                time.sleep(2 * (i + 1))
        return False

    def enviar(self, payload, tentativas=4):
        if not self.webhook:
            log.warning("DISCORD_WEBHOOK_URL não configurado; embed não enviado.")
            return False
        req = _req()
        payload = dict(payload)
        payload.setdefault("username", "DBD Coach · " + self.cfg.nome_servidor)
        for i in range(tentativas):
            try:
                r = req.post(self.webhook, json=payload, timeout=20, params={"wait": "true"})
                if r.status_code in (200, 204):
                    return True
                if r.status_code == 429:
                    espera = float((r.json() or {}).get("retry_after", 2))
                    time.sleep(min(30.0, espera) + 0.5)
                    continue
                if r.status_code >= 500:
                    time.sleep(2 * (i + 1))
                    continue
                log.error("Discord recusou o embed (%s): %s", r.status_code, r.text[:300])
                return False
            except Exception as e:
                log.warning("Falha ao falar com o Discord (%s); tentando de novo...", e)
                time.sleep(2 * (i + 1))
        return False


def _barra(v, n=10):
    cheio = int(round(max(0, min(100, v)) / 100 * n))
    return "█" * cheio + "░" * (n - cheio)


def _mmss(s):
    s = int(round(s or 0))
    return f"{s // 60}:{s % 60:02d}"


def _corta(txt, n):
    return txt if len(txt) <= n else txt[: n - 1] + "…"


RESULTADO_TXT = {"escapou": "🚪 **Escapou**", "sacrificado": "🪝 **Sacrificado**", "morto": "☠️ **Morto**",
                 "desconectou": "🔌 **Desconectou**"}


def montar_embed(jog, partida, coach, mmr, extra, cfg):
    m, sc = coach["metricas"], coach["scores"]
    assassino = coach.get("papel") == "assassino"
    nome_tier, _ = tier(mmr["mmr_novo"])
    vale = mmr.get("vale_mmr", True)
    cor = 0x8E44AD if not vale else 0x2ECC71 if mmr["delta"] >= 0 else 0xE74C3C
    sinal = "+" if mmr["delta"] >= 0 else ""
    calibrando = " · em calibração" if mmr["rd_novo"] > 200 else ""
    mencao_id = jog.get("discord_id") or (jog["id"] if RE_SNOWFLAKE.match(jog["id"]) else None)
    padrao = "Assassino" if assassino else "Sobrevivente"
    quem = f"<@{mencao_id}>" if mencao_id else f"**{jog.get('nome') or padrao}**"
    if assassino:
        tela = (partida.get("resultado_tela") or {}).get("texto")
        res_txt = f"💀 **{m['kills']} kill(s)**" + (f" · *{tela.title()}*" if tela else "")
    else:
        res_txt = RESULTADO_TXT.get(m.get("resultado_oficial") or "", "🚪 **Escapou**" if m["escapou"] else "☠️ **Não escapou**")
    linhas_desc = [f"{quem} · {'🔪 Assassino' if assassino else '🏃 Sobrevivente'} · {res_txt} · ⏱️ {_mmss(m['duracao_s'])}"]
    if partida.get("mapa"):
        linhas_desc.append(f"🗺️ {partida['mapa']}")
    linhas_desc.append(f"🧭 Perfil: **{' · '.join(coach['perfil'])}**")
    if m.get("personalizada"):
        linhas_desc.append("🤖 **Partida personalizada contra bots — não vale MMR** (só o coach).")
    elif m.get("bots"):
        linhas_desc.append(f"🤖 {m['bots']} jogador(es) viraram bot nesta partida — o MMR pesa menos.")

    nomes = NOMES_CAT_ASSASSINO if assassino else NOMES_CAT
    pesos = PESOS_ASSASSINO if assassino else PESOS
    desempenho = "\n".join(f"{nomes[k]:<13} {_barra(sc[k])} {sc[k]:>3.0f}" for k in pesos)
    if vale:
        valor_mmr = f"**{mmr['mmr_anterior']:.0f} → {mmr['mmr_novo']:.0f}**\n`{sinal}{mmr['delta']:.1f}` · {nome_tier}{calibrando}"
    else:
        valor_mmr = f"**{mmr['mmr_anterior']:.0f}** (sem alteração)\n{nome_tier}"
    campos = [
        {"name": f"📈 MMR de {padrao.lower()}", "value": valor_mmr, "inline": True},
        {"name": "🎯 Nota da partida", "value": f"**{coach['p_score']:.1f}** / 100", "inline": True},
    ]
    if extra.get("rank"):
        campos.append({"name": "🏆 Ranking", "value": f"#{extra['rank'][0]} de {extra['rank'][1]}", "inline": True})
    campos.append({"name": "📊 Desempenho", "value": "```\n" + desempenho + "\n```", "inline": False})
    if assassino:
        gens = "" if m["gens_restantes_fim"] is None else f" · ⚙️ {m['gens_restantes_fim']} gen(s) faltando no fim"
        pressao = (f"🪝 **{m['ganchos']}** gancho(s) em **{m['sobreviventes_enganchados']}** sobrevivente(s) · "
                   f"⏱️ 1º gancho {m['tempo_primeiro_gancho_txt']}\n"
                   f"🩸 {m['hits']} hit(s) · 🦶 {m['chutes_gerador']} gen(s) chutado(s) · 🪵 {m['destruicao']} quebrado(s){gens}")
        if m["n_chases"]:
            chase = (f"**{m['n_chases']}** chase(s) · média **{_mmss(m['chase_medio_s'])}** · maior {_mmss(m['maior_chase_s'])}"
                     + (f" · {m['chase_por_hit_s']:.0f}s por hit" if m["chase_por_hit_s"] else ""))
        else:
            chase = f"{m['encontrados']} sobrevivente(s) encontrado(s) · nenhuma chase fechada no feed"
        if m["sede_de_sangue"]:
            chase += f"\n🔴 Sede de Sangue {m['sede_de_sangue']}x"
        if m["usos_poder"] or m["hits_poder"]:
            prec = f" ({m['precisao_poder']:.0%})" if m["precisao_poder"] is not None else ""
            poder = (f"⚡ {m['usos_poder']} ataque(s) com o poder · " if m["usos_poder"] else "") + \
                f"🎯 {m['hits_poder']} acerto(s){prec}"
            if m["quedas_poder"]:
                poder += f" · {m['quedas_poder']} queda(s) direto pelo poder"
        else:
            poder = "Nenhum uso de poder reconhecido no feed."
        campos += [{"name": "🪝 Pressão e ganchos", "value": _corta(pressao, 1024), "inline": False},
                   {"name": "🏃 Chase", "value": _corta(chase, 1024), "inline": False},
                   {"name": "⚡ Poder", "value": _corta(poder, 1024), "inline": False}]
    else:
        chase = (f"**{m['n_chases']}** chase(s) · **{_mmss(m['chase_total_s'])}** no total · maior {_mmss(m['maior_chase_s'])}\n"
                 f"🪵 {m['pallets_usados']} pallet(s) · 💥 {m['stuns']} stun(s) · {m['seg_chase_por_pallet']:.0f}s de chase/pallet\n"
                 f"🪟 {m['fast_vaults']} fast vault(s) · 🛡️ {m['dead_hard']} Dead Hard · 🔦 {m['lanterna']} cegada(s)\n"
                 f"🩸 caiu {m['quedas']}x")
        gens_time = "" if m["gens_feitos_time"] is None else f" · time fez {m['gens_feitos_time']} gen(s)"
        objetivo = (f"⚙️ {_mmss(m['gen_s'])} em gerador · ✨ {m['great_skillchecks']} Great{gens_time}\n"
                    f"🦸 {m['unhooks']} resgate(s) ({m['unhooks_seguros']} seguro(s)) · 🩹 {_mmss(m['heal_s'])} curando")
        campos += [{"name": "🏃 Chase", "value": _corta(chase, 1024), "inline": False},
                   {"name": "🛠️ Objetivo e equipe", "value": _corta(objetivo, 1024), "inline": False}]
    elogios = [a for a in coach["achados"] if a["tipo"] == "elogio"][:4]
    alertas = [a for a in coach["achados"] if a["tipo"] == "alerta"][:4]
    if elogios:
        campos.append({"name": "✅ Pontos fortes",
                       "value": _corta("\n".join("• " + a["diagnostico"] for a in elogios), 1024), "inline": False})
    if alertas:
        campos.append({"name": "⚠️ Para melhorar",
                       "value": _corta("\n".join(f"• {a['diagnostico']}\n  ↳ *{a['dica']}*" for a in alertas), 1024),
                       "inline": False})
    fonte = {"hud": "chase medido na HUD", "feed": "chase medido pelo feed"}.get(extra.get("fonte_chase"),
                                                                             "chase estimado pelos pop-ups")
    idioma = {"pt": "português", "en": "inglês"}.get(partida.get("idioma"), "")
    conf = (f"HUD reconhecida em {extra['confianca']:.0%} do vídeo · escala {extra['escala']:.2f}x · {fonte}"
            + (f" · jogo em {idioma}" if idioma else "") + " · "
            + ("partida completa" if partida["completa"] else "**partida incompleta** (vale metade)"))
    campos.append({"name": "🔍 Confiabilidade", "value": _corta(conf, 1024), "inline": False})

    embed = {
        "title": _corta(f"{'🔪' if assassino else '🩸'} Análise de partida — {jog.get('nome') or padrao}", 256),
        "description": _corta("\n".join(linhas_desc), 4096),
        "color": cor,
        "fields": campos[:25],
        "footer": {"text": _corta(f"DBD Coach v{VERSAO} · partida {extra['indice'] + 1} de {extra['total']} do vídeo"
                                  f" · {mmr['partidas_jogadas']} partida(s) de {padrao.lower()}", 2048)},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if jog.get("avatar"):
        embed["thumbnail"] = {"url": jog["avatar"]}
    payload = {"embeds": [embed]}
    if cfg.mencionar and mencao_id:
        payload["content"] = f"<@{mencao_id}>"
        payload["allowed_mentions"] = {"users": [mencao_id]}
    else:
        payload["allowed_mentions"] = {"parse": []}
    return payload


def embed_falha(jogador_id, motivo, cfg, detalhe=None, nome=None, discord_id=None):
    alvo = discord_id or (jogador_id if jogador_id and RE_SNOWFLAKE.match(jogador_id) else None)
    mencao = f"<@{alvo}>" if alvo else None
    quem = mencao or (f"**{nome}**" if nome else None)
    embed = {"title": "⚠️ Vídeo não analisado", "color": 0x95A5A6,
             "description": _corta((quem + "\n" if quem else "") + motivo + (f"\n\n`{detalhe}`" if detalhe else ""), 4096),
             "footer": {"text": f"DBD Coach v{VERSAO}"}, "timestamp": datetime.now(timezone.utc).isoformat()}
    p = {"embeds": [embed], "allowed_mentions": {"users": [alvo]} if mencao and cfg.mencionar else {"parse": []}}
    if mencao and cfg.mencionar:
        p["content"] = mencao
    return p


ULTIMA_ANALISE = {}


def _janela_mais_cheia(a, b, tempos, largura):
    if b - a <= largura:
        return a, b
    melhor, ini = -1, b - largura
    passo = 1.0
    x = a
    while x + largura <= b + 1e-6:
        n = sum(1 for t in tempos if x <= t <= x + largura)
        if n >= melhor:
            melhor, ini = n, x
        x += passo
    return ini, ini + largura


def escolher_highlights(partida, coach, cfg):
    ev = sorted(partida["eventos"], key=lambda e: e["t"])
    t_ini = partida["inicio"]
    t_fim = next((e["t"] for e in ev if e["tipo"] == "MATCH_END"), partida["fim"])
    tempos = lambda *tps: [e["t"] for e in ev if e["tipo"] in tps]
    dentro = lambda lst, a, b, folga=1.0: [t for t in lst if a - folga <= t <= b + folga]
    seg_max = cfg.highlight_seg_max
    cand = []
    assassino = coach.get("papel") == "assassino"
    chases = [(a, b) for a, b in _intervalos(ev, "CHASE_START", "CHASE_END") if b - a >= 4]
    if assassino:
        hits, quedas_p = tempos("K_HIT", "KP_HIT"), tempos("KP_DOWN")
        ganchos = tempos("K_HOOK", "K_HOOK_NOVO")
        for a, b in chases:
            hs = dentro(hits, a, b, 2.0)
            qp = dentro(quedas_p, a, b, 2.0)
            derrubou = bool(qp) or len(hs) >= 2 or any(b <= g <= b + 40 for g in ganchos)
            if not hs:
                continue
            dur = b - a
            if dur < 8:
                continue
            if derrubou and dur <= 30:
                titulo = f"⚡ Insta-chase: derrubou em {_mmss(dur)}" + (" com o poder" if qp else "")
                nota = 100 - dur * 2 + 10 * len(qp)
            elif derrubou:
                titulo = f"🔪 Chase de {_mmss(dur)} até a queda"
                nota = 40 + 5 * len(hs)
            else:
                titulo, nota = f"🩸 Chase de {_mmss(dur)} com {len(hs)} hit(s)", 20 + 5 * len(hs)
            x, y = _janela_mais_cheia(a - 3, b + 2, hs + qp, seg_max)
            cand.append({"inicio": x, "fim": y, "titulo": titulo, "nota": nota})
        for t in quedas_p:
            if not any(c["inicio"] <= t <= c["fim"] for c in cand):
                cand.append({"inicio": t - 10, "fim": t + 3, "titulo": "🎯 Queda direto com o poder", "nota": 55})
        mortes = sorted(tempos("K_SACRIFICE", "K_KILL"))
        if coach["metricas"].get("kills", 0) >= 4 and mortes:
            cand.append({"inicio": mortes[-1] - 12, "fim": mortes[-1] + 4, "titulo": "💀 Último sacrifício (4K)",
                         "nota": 70})
    else:
        stuns, vaults = tempos("PALLET_STUN"), tempos("VAULT_FAST")
        dh, blinds = tempos("DEAD_HARD_DODGE"), tempos("FLASHLIGHT_BLIND")
        escapes = tempos("CHASE_ESCAPED")
        for a, b in chases:
            s_, v_, d_, l_ = (dentro(x, a, b) for x in (stuns, vaults, dh, blinds))
            despistou = any(abs(t - b) <= 3 for t in escapes)
            dur = b - a
            nota = min(dur, 120) * 0.5 + 12 * len(s_) + 5 * len(v_) + 10 * len(d_) + 10 * len(l_) + (15 if despistou else 0)
            extras = []
            if s_:
                extras.append(f"{len(s_)} stun(s)")
            if v_:
                extras.append(f"{len(v_)} fast vault(s)")
            if d_:
                extras.append("Dead Hard")
            if l_:
                extras.append("cegada")
            if despistou:
                extras.append("despistou o Killer")
            titulo = f"🏃 Chase de {_mmss(dur)}" + (f" ({', '.join(extras)})" if extras else "")
            x, y = _janela_mais_cheia(a - 2, b + 3, s_ + v_ + d_ + l_, seg_max)
            cand.append({"inicio": x, "fim": y, "titulo": titulo, "nota": nota})
        for t in tempos("FLASHLIGHT_BLIND"):
            if not any(c["inicio"] <= t <= c["fim"] for c in cand):
                cand.append({"inicio": t - 10, "fim": t + 4, "titulo": "🔦 Cegada de lanterna no Killer", "nota": 45})
        for t in tempos("PROTECTION_HIT"):
            if not any(c["inicio"] <= t <= c["fim"] for c in cand):
                cand.append({"inicio": t - 10, "fim": t + 4, "titulo": "🛡️ Hit de proteção pelo time", "nota": 35})
        for t in tempos("UNHOOK_SAFE"):
            cand.append({"inicio": t - 22, "fim": t - 8, "titulo": "🦸 Resgate do gancho", "nota": 30})
        for t in tempos("ESCAPE"):
            cand.append({"inicio": t - 18, "fim": t + 2, "titulo": "🚪 Fuga", "nota": 40})
    escolhidos = []
    for c in sorted(cand, key=lambda c: -c["nota"]):
        c["inicio"], c["fim"] = max(t_ini, c["inicio"]), min(t_fim + 3, c["fim"])
        if c["fim"] - c["inicio"] < 4:
            continue
        if any(c["inicio"] < e["fim"] and e["inicio"] < c["fim"] for e in escolhidos):
            continue
        escolhidos.append(c)
        if len(escolhidos) >= cfg.highlights_max:
            break
    return sorted(escolhidos, key=lambda c: c["inicio"])


def cortar_clipe(video, inicio, fim, destino, cfg):
    if not shutil.which("ffmpeg"):
        return None
    dur = max(1.0, fim - inicio)
    limite = cfg.highlight_mb * 1e6
    altura = cfg.highlight_altura
    for tentativa in range(3):
        kbps_total = limite * 8 / dur / 1000 * 0.92
        kbps_audio = 96 if kbps_total > 900 else 64
        kbps_video = max(250, int(kbps_total - kbps_audio))
        if kbps_video < 1200 and altura > 540:
            altura = 540
        cmd = ["ffmpeg", "-v", "error", "-y", "-nostdin", "-ss", f"{max(0.0, inicio):.2f}", "-i", video,
               "-t", f"{dur:.2f}", "-vf", f"scale=-2:{altura},fps=30", "-c:v", "libx264", "-preset", "veryfast",
               "-b:v", f"{kbps_video}k", "-maxrate", f"{int(kbps_video * 1.3)}k", "-bufsize", f"{kbps_video * 2}k",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", f"{kbps_audio}k", "-ac", "2",
               "-movflags", "+faststart", destino]
        try:
            subprocess.run(cmd, check=True, timeout=600)
        except Exception as e:
            log.warning("Falha ao cortar o clipe: %s", e)
            return None
        if os.path.exists(destino) and os.path.getsize(destino) <= limite:
            return destino
        limite *= 0.8
    return destino if os.path.exists(destino) and os.path.getsize(destino) <= cfg.highlight_mb * 1e6 else None


def gerar_highlights(video, partida, coach, cfg, pasta, prefixo="destaque"):
    clipes = []
    for i, h in enumerate(escolher_highlights(partida, coach, cfg)):
        destino = os.path.join(pasta, f"{prefixo}_{i + 1}.mp4")
        if cortar_clipe(video, h["inicio"], h["fim"], destino, cfg):
            clipes.append(dict(h, arquivo=destino))
    return clipes


def enviar_highlights(discord, clipes, jog, indice, total):
    enviados = 0
    for i, c in enumerate(clipes):
        quem = jog.get("nome") or ("Assassino" if jog.get("papel") == "assassino" else "Sobrevivente")
        texto = (f"🎬 **Destaque {i + 1}/{len(clipes)}** de **{quem}**"
                 + (f" (partida {indice + 1} de {total})" if total > 1 else "")
                 + f" · {c['titulo']} · ⏱️ {_mmss(c['inicio'])}–{_mmss(c['fim'])} do vídeo")
        if discord.enviar_arquivo({"content": _corta(texto, 1900)}, c["arquivo"], f"destaque_{i + 1}.mp4"):
            enviados += 1
    return enviados


class Recusa(Exception):

    def __init__(self, motivo, publico=True):
        super().__init__(motivo)
        self.publico = publico


def sha256_arquivo(caminho):
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def resultado_partida(coach):
    m = coach["metricas"]
    if coach.get("papel") == "assassino":
        return f"{m['kills']}k"
    return "escapou" if m["escapou"] else (m.get("resultado_oficial") or "nao_escapou")


def analisar_video(caminho, cfg):
    info = info_video(caminho)
    if not info["ok"]:
        raise Recusa("Não consegui abrir o vídeo. Envie em MP4 (H.264).")
    if info["duracao"] < cfg.duracao_rejeitar:
        raise Recusa(f"O vídeo tem só {info['duracao']:.0f}s. Envie a partida inteira.")
    if info["largura"] and info["altura"] and abs(info["largura"] / info["altura"] - 16 / 9) > 0.05:
        log.warning("Vídeo %dx%d não é 16:9; a HUD pode não bater.", info["largura"], info["altura"])
    det = DetectorHUD(fps_amostra=cfg.fps, tesseract_cmd=cfg.tesseract_cmd)
    if not det.ocr.ok:
        log.info("Tesseract não encontrado: sem leitura de gens/nome do mapa (o resto funciona).")
    t0 = time.time()
    eventos = det.analisar(caminho, processos=cfg.processos)
    log.info("Visão concluída em %.0fs · HUD %.0f%% · escala %.2fx · chase %s (%.0fs seus) · %d eventos",
             time.time() - t0, det.confianca_geral * 100, det.escala_hud, det.fonte_chase,
             det.info_chase.get("chase_pov_s", 0), len(eventos))
    if det.confianca_geral < cfg.confianca_min:
        raise Recusa("Não reconheci a HUD do Dead by Daylight neste vídeo "
                     f"({det.confianca_geral:.0%} dos frames). Grave a tela do jogo sem cortes nem "
                     "overlays e com a HUD visível.")
    ULTIMA_ANALISE["eventos"] = eventos
    partidas = dividir_partidas(eventos, det.duracao_s)
    if not partidas:
        raise Recusa("Não encontrei nenhuma partida com ações suficientes no vídeo.")
    for p in partidas:
        p["papel"], p["idioma"] = det.papel, det.idioma
        p["coach"] = analisar_coach(p["eventos"], det.papel, p)
        fim_visto = bool(p.get("resultado_tela") or p.get("placar"))
        p["completa"] = bool((p["inicio_visto"] or fim_visto) and p["duracao"] >= cfg.duracao_min)
    return det, partidas


def processar_video(caminho, jogador_id, cfg, banco=None, discord=None, video_key=None, nome=None, avatar=None,
                    discord_id=None, pasta_highlights=None):
    sha = sha256_arquivo(caminho)
    feitos, total_ant = (banco.partidas_ja_gravadas(sha) if banco else (set(), None))
    if banco and total_ant and len(feitos) >= total_ant:
        raise Recusa("Esse vídeo já foi enviado e analisado antes.")
    if banco and cfg.max_24h and banco.partidas_24h(jogador_id) >= cfg.max_24h:
        raise Recusa(f"Limite de {cfg.max_24h} partidas em 24h atingido. Tente mais tarde.")

    det, partidas = analisar_video(caminho, cfg)
    total = len(partidas)
    resultados = []
    for i, p in enumerate(partidas):
        coach = p["coach"]
        if i in feitos:
            log.info("Partida %d já gravada antes; pulando.", i + 1)
            continue
        m = coach["metricas"]
        papel = coach["papel"]
        res_s, peso_res, fator_c, vale = parametros_mmr(coach)
        dados = {"p_score": coach["p_score"], "video_key": video_key, "sha": sha, "indice": i, "total": total,
                 "resultado": resultado_partida(coach), "mapa": p.get("mapa"), "perfil": coach["perfil"],
                 "duracao": p["duracao"], "completa": p["completa"], "escala": det.escala_hud,
                 "scores": coach["scores"], "metricas": m, "achados": coach["achados"], "papel": papel,
                 "idioma": p.get("idioma"), "kills": m.get("kills"), "bots": m.get("bots", 0),
                 "personalizada": m.get("personalizada", False),
                 "resultado_tela": (p.get("resultado_tela") or {}).get("texto")}
        mmr_ref = banco.mmr_referencia(cfg.mmr_inicial, papel) if banco else cfg.mmr_inicial

        def calc(mmr, rd, dias):
            return calcular_mmr(mmr, rd, dias, mmr_ref, coach["p_score"], res_s, peso_res,
                                det.confianca_geral, p["completa"], cfg, fator_c, vale)

        if banco:
            res = banco.registrar_partida(jogador_id, nome, dados, calc, cfg)
            rank = banco.ranking(jogador_id, papel)
        else:
            res = calc(cfg.mmr_inicial, cfg.rd_inicial, 0.0)
            res["partidas_jogadas"] = 1
            rank = None
        payload = montar_embed({"id": jogador_id, "nome": nome, "avatar": avatar, "discord_id": discord_id},
                               p, coach, res,
                               {"indice": i, "total": total, "confianca": det.confianca_geral,
                                "escala": det.escala_hud, "fonte_chase": det.fonte_chase,
                                "rank": rank if rank and rank[0] else None}, cfg)
        enviado = discord.enviar(payload) if discord else False
        clipes = []
        if cfg.highlights and (discord or pasta_highlights):
            os.makedirs(cfg.pasta_tmp, exist_ok=True)
            pasta = pasta_highlights or tempfile.mkdtemp(prefix="destaques_", dir=cfg.pasta_tmp)
            try:
                clipes = gerar_highlights(caminho, p, coach, cfg, pasta, f"partida{i + 1}_destaque")
                if discord and enviado and clipes:
                    n = enviar_highlights(discord, clipes, {"nome": nome, "papel": coach["papel"]}, i, total)
                    log.info("Destaques: %d de %d clipe(s) enviados.", n, len(clipes))
                elif clipes:
                    log.info("Destaques salvos em %s: %s", pasta, ", ".join(os.path.basename(c["arquivo"]) for c in clipes))
            except Exception as e:
                log.warning("Não consegui gerar os destaques: %s", e)
            finally:
                if not pasta_highlights:
                    shutil.rmtree(pasta, ignore_errors=True)
        log.info("Partida %d/%d · %s · P=%.1f · MMR %.0f → %.0f (%+.1f) · embed %s",
                 i + 1, total, resultado_partida(coach), coach["p_score"], res["mmr_anterior"],
                 res["mmr_novo"], res["delta"], "enviado" if enviado else "não enviado")
        resultados.append({"partida": {k: v for k, v in p.items() if k != "eventos"}, "mmr": res, "payload": payload,
                           "destaques": [{k: v for k, v in c.items() if k != "arquivo"} for c in clipes]})
    return resultados


def processar_item(obj, fila, banco, discord, cfg):
    key = obj["Key"]
    info = ler_chave(key)
    jogador_id = info["jogador_id"] if info else None
    log.info("=" * 70)
    log.info("Novo vídeo na fila: %s (%.0f MB)", key, obj.get("Size", 0) / 1e6)

    reiv = fila.reivindicar(key)
    if not reiv:
        log.info("Outro worker já pegou %s.", key)
        return
    key_proc, tentativa, tamanho = reiv
    caminho, nome, discord_id = None, None, None
    try:
        if not info:
            raise Recusa("Nome de arquivo fora do padrão <jogadorId>_<timestamp>_<nome>.", publico=False)
        if not RE_ID.match(jogador_id) or (cfg.exigir_snowflake and not RE_SNOWFLAKE.match(jogador_id)):
            raise Recusa(f"ID de jogador inválido ({jogador_id}). Faça login no site para enviar.", publico=False)
        if not key.lower().endswith(EXT_VIDEO):
            raise Recusa("Formato não suportado. Envie MP4.")
        if tamanho > cfg.max_gb * 1e9:
            raise Recusa(f"Vídeo grande demais ({tamanho / 1e9:.1f} GB). Limite: {cfg.max_gb:.0f} GB.")
        meta = fila.ler_meta(key)
        if meta.get("uid") and meta["uid"] != jogador_id:
            raise Recusa("A ficha do envio não bate com o vídeo.", publico=False)
        nome = meta.get("nome")
        discord_id = jogador_id if RE_SNOWFLAKE.match(jogador_id) else (
            str(meta["discord_id"]) if RE_SNOWFLAKE.match(str(meta.get("discord_id", ""))) else None)
        usuario = discord.usuario(discord_id) if (discord and discord_id) else None
        if usuario and not usuario.get("existe"):
            raise Recusa(f"O ID {discord_id} não existe no Discord.", publico=False)
        nome = (usuario or {}).get("nome") or nome
        avatar = (usuario or {}).get("avatar") or meta.get("foto")

        os.makedirs(cfg.pasta_tmp, exist_ok=True)
        ext = os.path.splitext(key)[1] or ".mp4"
        fd, caminho = tempfile.mkstemp(prefix="partida_", suffix=ext, dir=cfg.pasta_tmp)
        os.close(fd)
        t0 = time.time()
        fila.baixar(key_proc, caminho)
        log.info("Baixado em %.0fs.", time.time() - t0)

        processar_video(caminho, jogador_id, cfg, banco, discord, video_key=key,
                        nome=nome, avatar=avatar, discord_id=discord_id)
        fila.apagar_meta(key)
        if cfg.apagar_video:
            fila.apagar(key_proc)
            log.info("Vídeo apagado do armazenamento.")
        else:
            fila.mover_para_falhas(key_proc, "processado (APAGAR_VIDEO=0)")
    except Recusa as r:
        log.warning("Recusado: %s", r)
        fila.mover_para_falhas(key_proc, str(r))
        if info:
            fila.apagar_meta(key)
        if r.publico and discord:
            discord.enviar(embed_falha(jogador_id, str(r), cfg, nome=nome, discord_id=discord_id))
    except Exception as e:
        log.error("Erro processando %s:\n%s", key, traceback.format_exc())
        if tentativa >= 3:
            fila.mover_para_falhas(key_proc, f"erro interno: {type(e).__name__}: {e}")
            if discord:
                discord.enviar(embed_falha(jogador_id, "Tive um erro interno analisando seu vídeo. "
                                                        "A staff foi avisada pelo log.", cfg, type(e).__name__,
                                           nome=nome, discord_id=discord_id))
        else:
            log.info("Tentativa %d falhou; o vídeo volta para a fila mais tarde.", tentativa)
    finally:
        if caminho and os.path.exists(caminho):
            try:
                os.remove(caminho)
            except OSError:
                pass


PARAR = {"pedido": False}


def _sinal(signum, frame):
    if PARAR["pedido"]:
        log.warning("Saindo agora.")
        sys.exit(1)
    PARAR["pedido"] = True
    log.warning("Parada pedida: termino o vídeo atual e saio (Ctrl+C de novo força).")


def checar(cfg):
    ok = True
    print(f"DBD Coach worker v{VERSAO}")
    for (papel, idioma), pastas in CONJUNTOS.items():
        n = sum(len(glob.glob(os.path.join(TEMPLATES_DIR, pa, "*.png"))) for pa in pastas)
        print(f"  templates {papel[:5]}/{idioma} {'OK (' + str(n) + ')' if n else 'FALTANDO em ' + TEMPLATES_DIR}")
    n = len(glob.glob(os.path.join(TEMPLATES_DIR, "comum", "placar", "*.png")))
    print(f"  templates placar . {'OK (' + str(n) + ')' if n else 'FALTANDO'}")
    print(f"  ffmpeg ........... {'OK' if shutil.which('ffmpeg') else 'não encontrado (vai usar OpenCV, mais lento)'}")
    ocr = _OCR(cfg.tesseract_cmd)
    print(f"  tesseract ........ {'OK (' + ocr.caminho + ')' if ocr.ok else 'não encontrado (opcional) — ' + ocr.motivo}")
    if ocr.ok:
        try:
            langs = set(ocr.pt.get_languages(config=""))
        except Exception:
            langs = set()
        falta = [l for l in ("eng", "por") if l not in langs]
        print(f"  idiomas do OCR ... {'OK (eng + por)' if not falta else 'faltando: ' + ', '.join(falta) + ' — reinstale o Tesseract marcando Portuguese (VPS: apt install tesseract-ocr-por)'}")
    mem = _memoria_mb()
    print(f"  processos ........ {cfg.processos}   fps de amostra: {cfg.fps}   RAM: {str(mem) + ' MB' if mem else '?'}")
    falt = cfg.faltando("fila")
    print(f"  .env ............. {'OK' if not falt else 'faltando: ' + ', '.join(falt)}")
    ok &= not falt
    if cfg.database_url:
        try:
            Banco(cfg.database_url).preparar()
            print("  banco ............ OK")
        except Exception as e:
            ok = False
            print(f"  banco ............ ERRO: {str(e).strip().splitlines()[0]}")
            dica = dica_banco(cfg.database_url, e)
            if dica:
                print("    " + dica)
    if (cfg.armazenamento == "site" and cfg.site_url and cfg.worker_token) or (
            cfg.armazenamento != "site" and cfg.r2_endpoint and cfg.r2_key):
        rotulo = "site" if cfg.armazenamento == "site" else "R2"
        try:
            n = len(criar_fila(cfg).pendentes())
            print(f"  {rotulo} {'.' * (17 - len(rotulo))} OK ({n} vídeo(s) na fila)")
        except Exception as e:
            ok = False
            print(f"  {rotulo} {'.' * (17 - len(rotulo))} ERRO: {e}")
    print(f"  discord token .... {'OK (confere IDs)' if cfg.discord_token else 'não definido (opcional)'}")
    return ok


def dica_banco(url, erro):
    txt = str(erro)
    if "supabase.co" in (url or "") and ".pooler." not in (url or "") and (
            any(k in txt.lower() for k in ("unreachable", "could not translate host", "resolve host",
                                             "name or service not known", "no address associated"))):
        return ("DICA: o endereço direto do Supabase (db.<projeto>.supabase.co) só funciona por IPv6, e esta "
                "hospedagem não tem IPv6. No Supabase vá em Project Settings > Database > Connection string > "
                "'Session pooler' e use essa URL no DATABASE_URL (host *.pooler.supabase.com, usuário "
                "postgres.<projeto>).")
    if "password authentication failed" in txt:
        return "DICA: senha do banco errada no DATABASE_URL (caracteres especiais na senha precisam de URL-encode)."
    return None


def loop(cfg, uma_vez=False):
    falt = cfg.faltando("fila")
    if falt:
        log.error("Faltam variáveis no .env: %s", ", ".join(falt))
        sys.exit(2)
    banco = Banco(cfg.database_url)
    espera = 15
    while True:
        try:
            banco.preparar()
            break
        except Exception as e:
            log.error("Não consegui conectar no banco: %s", str(e).strip().splitlines()[0])
            dica = dica_banco(cfg.database_url, e)
            if dica:
                log.error(dica)
            if uma_vez or PARAR["pedido"]:
                sys.exit(2)
            log.info("Tentando de novo em %d s...", espera)
            for _ in range(espera):
                if PARAR["pedido"]:
                    sys.exit(2)
                time.sleep(1)
            espera = min(300, espera * 2)
    fila = criar_fila(cfg)
    discord = Discord(cfg)
    log.info("=" * 70)
    log.info("O OLHO DA ENTIDADE ESTÁ ATIVO · v%s · %s · %d processo(s) · %.1f fps",
             VERSAO, fila.descricao, cfg.processos, cfg.fps)
    log.info("=" * 70)
    fila.recuperar_orfaos()
    ultimo_orfao = time.time()
    while not PARAR["pedido"]:
        try:
            if time.time() - ultimo_orfao > 900:
                fila.recuperar_orfaos()
                ultimo_orfao = time.time()
            pend = fila.pendentes()
            if pend:
                processar_item(pend[0], fila, banco, discord, cfg)
                continue
            if uma_vez:
                break
        except Exception as e:
            log.error("Erro no loop da fila: %s", e)
        for _ in range(cfg.intervalo_fila):
            if PARAR["pedido"]:
                break
            time.sleep(1)
    log.info("Worker parado.")


def main():
    configurar_log()
    ap = argparse.ArgumentParser(description="DBD Coach — worker de análise de partidas")
    ap.add_argument("--uma-vez", action="store_true", help="processa a fila atual e sai")
    ap.add_argument("--local", metavar="VIDEO", help="analisa um arquivo local (sem fila)")
    ap.add_argument("--jogador", help="ID do jogador: uid do Firebase ou ID do Discord (com --local)")
    ap.add_argument("--nome", help="nome do jogador no embed (com --local)")
    ap.add_argument("--gravar", action="store_true", help="com --local: grava no banco e posta no Discord")
    ap.add_argument("--enviar", action="store_true", help="com --local: posta o embed no Discord SEM gravar no banco")
    ap.add_argument("--json", help="com --local: salva eventos/resultado neste arquivo")
    ap.add_argument("--destaques", metavar="PASTA", help="com --local: salva os clipes de destaque nesta pasta")
    ap.add_argument("--teste-discord", action="store_true", help="manda um embed de teste no webhook")
    ap.add_argument("--checar", action="store_true", help="confere configuração e conexões")
    a = ap.parse_args()
    cfg = Config()
    signal.signal(signal.SIGINT, _sinal)
    try:
        signal.signal(signal.SIGTERM, _sinal)
    except (AttributeError, ValueError):
        pass

    if a.checar:
        sys.exit(0 if checar(cfg) else 1)
    if a.teste_discord:
        ok = Discord(cfg).enviar({"embeds": [{"title": "🩸 DBD Coach conectado",
                                              "description": "Webhook funcionando. As análises vão aparecer aqui.",
                                              "color": 0x8E44AD, "footer": {"text": f"DBD Coach v{VERSAO}"}}]})
        print("Embed de teste enviado." if ok else "Falhou — confira DISCORD_WEBHOOK_URL.")
        sys.exit(0 if ok else 1)
    if a.local:
        jog = a.jogador or "000000000000000000"
        banco = discord = None
        if a.gravar:
            falt = cfg.faltando("gravar")
            if falt:
                log.error("Para --gravar faltam no .env: %s", ", ".join(falt))
                sys.exit(2)
            banco = Banco(cfg.database_url)
            banco.preparar()
            discord = Discord(cfg)
        elif a.enviar:
            if cfg.faltando("discord"):
                log.error("Para --enviar falta DISCORD_WEBHOOK_URL no .env")
                sys.exit(2)
            discord = Discord(cfg)
            log.info("Modo teste: o embed vai para o Discord, o banco NÃO é usado (MMR parte de %.0f).", cfg.mmr_inicial)
        u = Discord(cfg).usuario(jog) if (cfg.discord_token and RE_SNOWFLAKE.match(jog)) else None
        try:
            if a.destaques:
                os.makedirs(a.destaques, exist_ok=True)
            res = processar_video(a.local, jog, cfg, banco, discord, video_key=os.path.basename(a.local),
                                  nome=(u or {}).get("nome") or a.nome, avatar=(u or {}).get("avatar"),
                                  discord_id=jog if RE_SNOWFLAKE.match(jog) else None, pasta_highlights=a.destaques)
        except Recusa as r:
            log.error("Recusado: %s", r)
            sys.exit(1)
        for r in res:
            emb = r["payload"]["embeds"][0]
            print("\n" + emb["title"])
            print(emb["description"])
            for f in emb["fields"]:
                print(f"\n[{f['name']}]\n{f['value']}")
            for d in r.get("destaques", []):
                print(f"🎬 {d['titulo']} · {_mmss(d['inicio'])}–{_mmss(d['fim'])}")
        if a.json:
            json.dump({"partidas": res, "eventos_video": ULTIMA_ANALISE.get("eventos", [])},
                      open(a.json, "w", encoding="utf-8"), indent=1, ensure_ascii=False, default=float)
        return
    loop(cfg, uma_vez=a.uma_vez)


if __name__ == "__main__":
    main()
