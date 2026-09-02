import os
import json
import time
import logging
import threading
import secrets
import re
from datetime import datetime, timedelta

import requests
import pytz
import numpy as np
import psycopg2
from psycopg2.extras import RealDictCursor

from flask import (
    Flask,
    render_template_string,
    request,
    jsonify,
    session,
    redirect,
    abort,
)
from werkzeug.security import generate_password_hash, check_password_hash


# ============================================================
# CONFIGURAÇÃO GERAL
# ============================================================

FUSO_SP = pytz.timezone("America/Sao_Paulo")

def agora_brasilia():
    return datetime.now(FUSO_SP)


# ============================================================
# VARIÁVEIS DE AMBIENTE
# ============================================================

TOKEN_TELEGRAM = os.getenv("TOKEN_TELEGRAM", "").strip()
CHAT_ID_TELEGRAM = os.getenv("CHAT_ID_TELEGRAM", "").strip()

ADMIN_EMAIL = os.getenv(
    "ADMIN_EMAIL",
    "admin@vision.com"
).strip().lower()

# OBRIGATÓRIO EM PRODUÇÃO
APP_SECRET = os.getenv("FLASK_SECRET", "").strip()

if not APP_SECRET:
    raise RuntimeError(
        "ERRO DE SEGURANÇA: configure FLASK_SECRET no ambiente."
    )

if len(APP_SECRET) < 32:
    raise RuntimeError(
        "ERRO DE SEGURANÇA: FLASK_SECRET deve possuir pelo menos 32 caracteres."
    )


DB_URL = (
    os.getenv("DB_URL")
    or os.getenv("DATABASE_URL")
    or ""
).strip()

if not DB_URL:
    raise RuntimeError(
        "ERRO: configure DB_URL ou DATABASE_URL."
    )

DB_SSLMODE = os.getenv("DB_SSLMODE", "require").strip()


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

app.secret_key = APP_SECRET

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=1024 * 1024,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vision_pro")


# ============================================================
# SEGURANÇA / RATE LIMIT
# ============================================================

RATE_LIMIT = {}
RATE_LIMIT_LOCK = threading.Lock()

LOGIN_MAX_ATTEMPTS = 8
LOGIN_WINDOW_SECONDS = 300


def get_client_ip():
    """
    Em produção, prefira configurar o proxy/reverse proxy corretamente.
    Não confie cegamente em X-Forwarded-For.
    """
    return request.remote_addr or "unknown"


def rate_limit_login(ip):
    now = time.time()

    with RATE_LIMIT_LOCK:
        data = RATE_LIMIT.get(ip)

        if not data:
            RATE_LIMIT[ip] = {
                "count": 1,
                "first": now,
            }
            return True

        if now - data["first"] > LOGIN_WINDOW_SECONDS:
            RATE_LIMIT[ip] = {
                "count": 1,
                "first": now,
            }
            return True

        if data["count"] >= LOGIN_MAX_ATTEMPTS:
            return False

        data["count"] += 1
        return True


def limpar_rate_limit_periodicamente():
    while True:
        time.sleep(600)

        now = time.time()

        with RATE_LIMIT_LOCK:
            remover = [
                ip
                for ip, data in RATE_LIMIT.items()
                if now - data["first"] > LOGIN_WINDOW_SECONDS
            ]

            for ip in remover:
                RATE_LIMIT.pop(ip, None)


threading.Thread(
    target=limpar_rate_limit_periodicamente,
    daemon=True
).start()


# ============================================================
# CSRF
# ============================================================

def csrf_token():
    token = session.get("_csrf_token")

    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token

    return token


app.jinja_env.globals["csrf_token"] = csrf_token


def validar_csrf():
    token_session = session.get("_csrf_token")
    token_form = request.form.get("_csrf_token", "")

    if (
        not token_session
        or not token_form
        or not secrets.compare_digest(token_session, token_form)
    ):
        abort(400, description="Token CSRF inválido.")


# ============================================================
# HEADERS DE SEGURANÇA
# ============================================================

@app.after_request
def security_headers(response):

    response.headers["X-Content-Type-Options"] = "nosniff"

    response.headers["X-Frame-Options"] = "SAMEORIGIN"

    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    response.headers[
        "Permissions-Policy"
    ] = "camera=(), microphone=(), geolocation=()"

    response.headers[
        "Content-Security-Policy"
    ] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-src https://qxbroker.com https://*.qxbroker.com "
        "https://iqoption.com https://*.iqoption.com; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )

    if request.is_secure:
        response.headers[
            "Strict-Transport-Security"
        ] = "max-age=31536000; includeSubDomains"

    return response


# ============================================================
# BANCO
# ============================================================

def get_db_connection():
    return psycopg2.connect(
        DB_URL,
        sslmode=DB_SSLMODE,
        connect_timeout=10,
        application_name="vision_pro_v3"
    )


def init_db():

    conn = None
    cur = None

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                email VARCHAR(255) PRIMARY KEY,
                senha VARCHAR(255) NOT NULL,
                criado_em VARCHAR(50) NOT NULL,
                wins INT NOT NULL DEFAULT 0,
                reds INT NOT NULL DEFAULT 0,
                winrate DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                ips_autorizados TEXT NOT NULL DEFAULT '[]'
            );
        """)

        cur.execute("""
            ALTER TABLE usuarios
            ADD COLUMN IF NOT EXISTS ips_autorizados TEXT
            NOT NULL DEFAULT '[]';
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS historico_sinais (
                id BIGSERIAL PRIMARY KEY,
                user_email VARCHAR(255) NOT NULL,
                sinal VARCHAR(255) NOT NULL,
                resultado VARCHAR(50) NOT NULL,
                criado_em TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_historico_user
            ON historico_sinais(user_email);
        """)

        conn.commit()

        log.info("Banco de dados inicializado.")

    except Exception:
        if conn:
            conn.rollback()

        log.exception("Erro inicializando banco.")

        raise

    finally:
        if cur:
            cur.close()

        if conn:
            conn.close()


init_db()


# ============================================================
# UTILITÁRIOS DE BANCO
# ============================================================

def carregar_usuarios():

    conn = None
    cur = None

    try:
        conn = get_db_connection()

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            SELECT
                email,
                senha,
                criado_em,
                wins,
                reds,
                winrate,
                ips_autorizados
            FROM usuarios
            ORDER BY email;
        """)

        rows = cur.fetchall()

        resultado = {}

        for row in rows:

            email = str(
                row.get("email", "")
            ).strip().lower()

            if not email:
                continue

            item = dict(row)

            ips = parse_ips(
                item.get("ips_autorizados")
            )

            item["ips_list"] = ips

            item["ips_Formatados"] = (
                ", ".join(ips)
                if ips
                else "Nenhum (Livre)"
            )

            resultado[email] = item

        return resultado

    except Exception:
        log.exception("Erro carregando usuários.")
        return {}

    finally:
        if cur:
            cur.close()

        if conn:
            conn.close()


def obter_usuario(email):

    email = email.strip().lower()

    conn = None
    cur = None

    try:
        conn = get_db_connection()

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            SELECT *
            FROM usuarios
            WHERE email = %s
            LIMIT 1;
        """, (email,))

        row = cur.fetchone()

        return dict(row) if row else None

    except Exception:
        log.exception("Erro obtendo usuário.")
        return None

    finally:
        if cur:
            cur.close()

        if conn:
            conn.close()


def parse_ips(raw):

    if not raw:
        return []

    try:

        data = json.loads(raw)

        if not isinstance(data, list):
            return []

        resultado = []

        for ip in data:

            if isinstance(ip, str) and len(ip) <= 64:
                resultado.append(ip)

        return resultado[:2]

    except Exception:
        return []


def validar_email(email):

    email = email.strip().lower()

    if len(email) > 255:
        return False

    padrao = (
        r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
        r"@[A-Za-z0-9-]+"
        r"(?:\.[A-Za-z0-9-]+)+$"
    )

    return bool(re.match(padrao, email))


def validar_senha(password):

    if not password:
        return False

    if len(password) < 8:
        return False

    if len(password) > 128:
        return False

    return True


def salvar_usuario(
    email,
    senha,
    data=None,
    ip_inicial=None
):

    email = email.strip().lower()

    if not validar_email(email):
        raise ValueError("E-mail inválido.")

    if not validar_senha(senha):
        raise ValueError(
            "A senha deve possuir entre 8 e 128 caracteres."
        )

    data_criacao = (
        data
        if data
        else agora_brasilia().strftime("%Y-%m-%d")
    )

    senha_hash = generate_password_hash(
        senha,
        method="scrypt"
    )

    ips = []

    if ip_inicial:
        ips.append(ip_inicial)

    ips = ips[:2]

    conn = None
    cur = None

    try:

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO usuarios (
                email,
                senha,
                criado_em,
                wins,
                reds,
                winrate,
                ips_autorizados
            )
            VALUES (
                %s, %s, %s, 0, 0, 0.0, %s
            )
            ON CONFLICT (email)
            DO UPDATE SET senha = EXCLUDED.senha;
        """, (
            email,
            senha_hash,
            data_criacao,
            json.dumps(ips)
        ))

        conn.commit()

    except Exception:
        if conn:
            conn.rollback()

        raise

    finally:
        if cur:
            cur.close()

        if conn:
            conn.close()


# ============================================================
# IP AUTORIZADO
# ============================================================

def verificar_ip_usuario(email):

    usuario = obter_usuario(email)

    if not usuario:
        return False

    ips = parse_ips(
        usuario.get("ips_autorizados")
    )

    # Nenhum IP cadastrado = acesso livre
    if not ips:
        return True

    ip_atual = get_client_ip()

    return ip_atual in ips


def registrar_ip_usuario(email):

    ip = get_client_ip()

    conn = None
    cur = None

    try:

        conn = get_db_connection()

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            SELECT ips_autorizados
            FROM usuarios
            WHERE email = %s
            FOR UPDATE;
        """, (email,))

        row = cur.fetchone()

        if not row:
            conn.rollback()
            return False

        ips = parse_ips(
            row["ips_autorizados"]
        )

        if not ips:
            ips = [ip]

        elif ip not in ips:

            if len(ips) >= 2:
                conn.rollback()
                return False

            ips.append(ip)

        cur.execute("""
            UPDATE usuarios
            SET ips_autorizados = %s
            WHERE email = %s;
        """, (
            json.dumps(ips[:2]),
            email
        ))

        conn.commit()

        return True

    except Exception:
        if conn:
            conn.rollback()

        log.exception("Erro registrando IP.")
        return False

    finally:
        if cur:
            cur.close()

        if conn:
            conn.close()


# ============================================================
# ASSINATURA
# ============================================================

def verificar_assinatura(email):

    email = email.strip().lower()

    if email == ADMIN_EMAIL:
        return True, 9999

    usuario = obter_usuario(email)

    if not usuario:
        return False, 0

    try:

        criado_str = str(
            usuario["criado_em"]
        ).split("T")[0]

        data_criacao = datetime.strptime(
            criado_str,
            "%Y-%m-%d"
        )

        hoje = agora_brasilia().replace(
            tzinfo=None
        )

        dias_passados = (
            hoje - data_criacao
        ).days

        dias_restantes = 30 - dias_passados

        return (
            dias_restantes > 0,
            max(dias_restantes, 0)
        )

    except Exception:
        log.exception(
            "Erro verificando assinatura."
        )

        return False, 0


# ============================================================
# ESTATÍSTICAS
# ============================================================

def atualizar_estatisticas_usuario(
    email,
    resultado
):

    email = email.strip().lower()

    if resultado not in ("win", "red"):
        return False

    conn = None
    cur = None

    try:

        conn = get_db_connection()

        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            SELECT wins, reds
            FROM usuarios
            WHERE email = %s
            FOR UPDATE;
        """, (email,))

        row = cur.fetchone()

        if not row:
            conn.rollback()
            return False

        wins = int(row["wins"] or 0)
        reds = int(row["reds"] or 0)

        if resultado == "win":
            wins += 1
        else:
            reds += 1

        total = wins + reds

        winrate = (
            round((wins / total) * 100, 1)
            if total
            else 0.0
        )

        cur.execute("""
            UPDATE usuarios
            SET
                wins = %s,
                reds = %s,
                winrate = %s
            WHERE email = %s;
        """, (
            wins,
            reds,
            winrate,
            email
        ))

        conn.commit()

        return True

    except Exception:
        if conn:
            conn.rollback()

        log.exception(
            "Erro atualizando estatísticas."
        )

        return False

    finally:
        if cur:
            cur.close()

        if conn:
            conn.close()


def zerar_estatisticas_usuario(email):

    conn = None
    cur = None

    try:

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            UPDATE usuarios
            SET
                wins = 0,
                reds = 0,
                winrate = 0.0
            WHERE email = %s;
        """, (
            email.strip().lower(),
        ))

        conn.commit()

    except Exception:
        if conn:
            conn.rollback()

        log.exception(
            "Erro zerando estatísticas."
        )

    finally:
        if cur:
            cur.close()

        if conn:
            conn.close()


def registrar_sinal_bd(
    email,
    sinal,
    resultado="Analisando..."
):

    conn = None
    cur = None

    try:

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO historico_sinais (
                user_email,
                sinal,
                resultado
            )
            VALUES (%s, %s, %s);
        """, (
            email.strip().lower(),
            sinal,
            resultado
        ))

        conn.commit()

    except Exception:
        if conn:
            conn.rollback()

        log.exception(
            "Erro registrando sinal."
        )

    finally:
        if cur:
            cur.close()

        if conn:
            conn.close()


# ============================================================
# TELEGRAM
# ============================================================

ULTIMO_MSG_ID_TELEGRAM = None


def enviar_telegram(
    mensagem,
    auto_delete=None,
    user_solicitante=None
):

    global ULTIMO_MSG_ID_TELEGRAM

    if user_solicitante != ADMIN_EMAIL:
        return None

    if not TOKEN_TELEGRAM:
        log.warning(
            "TOKEN_TELEGRAM não configurado."
        )
        return None

    if not CHAT_ID_TELEGRAM:
        return None

    try:

        url = (
            "https://api.telegram.org/"
            f"bot{TOKEN_TELEGRAM}/sendMessage"
        )

        payload = {
            "chat_id": CHAT_ID_TELEGRAM,
            "text": mensagem,
            "parse_mode": "HTML",
        }

        response = requests.post(
            url,
            json=payload,
            timeout=8
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("ok"):
            return None

        msg_id = data["result"]["message_id"]

        if any(
            termo in mensagem
            for termo in (
                "SINAL CONFIRMADO",
                "🔥",
                "🎯"
            )
        ):
            ULTIMO_MSG_ID_TELEGRAM = msg_id

        if auto_delete:
            threading.Thread(
                target=deletar_mensagem_atrasada,
                args=(msg_id, auto_delete),
                daemon=True
            ).start()

        return msg_id

    except Exception:
        log.exception(
            "Erro enviando Telegram."
        )

        return None


def deletar_mensagem_telegram(msg_id):

    if not TOKEN_TELEGRAM:
        return

    if not CHAT_ID_TELEGRAM:
        return

    if not msg_id:
        return

    try:

        url = (
            "https://api.telegram.org/"
            f"bot{TOKEN_TELEGRAM}/deleteMessage"
        )

        requests.post(
            url,
            json={
                "chat_id": CHAT_ID_TELEGRAM,
                "message_id": msg_id
            },
            timeout=8
        )

    except Exception:
        log.exception(
            "Erro removendo mensagem Telegram."
        )


def deletar_mensagem_atrasada(
    msg_id,
    delay
):

    if delay > 0:
        time.sleep(delay)

    deletar_mensagem_telegram(msg_id)


# ============================================================
# ESTADO DO ROBÔ POR USUÁRIO
# ============================================================

ESTADOS = {}
ESTADOS_LOCK = threading.RLock()


def estado_padrao():

    return {
        "bot_iniciado": False,
        "bot_pausado": True,

        "timeframe": 5,
        "mercado": "TODOS",
        "estrategia": "TODAS",

        "aguardando_resultado": False,

        "ultimo_sinal": "Aguardando Comando...",
        "sinal_display": None,

        "ativo_atual": "AGUARDANDO...",

        "ultimo_ativo_sinal": None,
        "ultimo_resultado": None,

        "entrada": None,
        "saida": None,
    }


def obter_estado(email):

    with ESTADOS_LOCK:

        if email not in ESTADOS:
            ESTADOS[email] = estado_padrao()

        return ESTADOS[email]


def limpar_estado(email):

    with ESTADOS_LOCK:
        ESTADOS.pop(email, None)


# ============================================================
# ATIVOS
# ============================================================

ATIVOS_BASE = {

    "FOREX": [
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "AUDUSD",
        "USDCAD",
        "USDCHF",
        "NZDUSD",
        "EURGBP",
        "EURJPY",
        "GBPJPY",
        "AUDJPY",
    ],

    "CRIPTO": [
        "BTCUSD",
        "ETHUSD",
        "SOLUSD",
        "BNBUSD",
        "XRPUSD",
        "ADAUSD",
        "AVAXUSD",
        "LINKUSD",
        "DOGEUSD",
    ],

    "OTC": [
        "EURUSD_OTC",
        "GBPUSD_OTC",
        "USDJPY_OTC",
    ],
}


MAPA_TICKERS = {}

for par in ATIVOS_BASE["FOREX"]:
    MAPA_TICKERS[par] = f"{par}=X"

for par in ATIVOS_BASE["CRIPTO"]:
    MAPA_TICKERS[par] = (
        par.replace("USD", "USDT")
    )


# ============================================================
# API DE DADOS
# ============================================================

def get_data_v2(
    ticker,
    tf,
    mercado="CRIPTO"
):

    if mercado == "OTC":
        return None

    if mercado == "FOREX":

        try:

            url = (
                "https://query2.finance.yahoo.com/"
                "v8/finance/chart/"
                f"{ticker}"
            )

            params = {
                "interval": f"{tf}m",
                "range": "1d",
            }

            headers = {
                "User-Agent":
                    "VisionProV3/1.0",
                "Accept":
                    "application/json",
            }

            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=8
            )

            if response.status_code != 200:
                return None

            payload = response.json()

            result_list = (
                payload
                .get("chart", {})
                .get("result")
            )

            if not result_list:
                return None

            result = result_list[0]

            timestamps = result.get(
                "timestamp"
            )

            quote = (
                result
                .get("indicators", {})
                .get("quote", [{}])[0]
            )

            if not timestamps or not quote:
                return None

            o = np.array(
                quote.get("open", []),
                dtype=float
            )

            h = np.array(
                quote.get("high", []),
                dtype=float
            )

            l = np.array(
                quote.get("low", []),
                dtype=float
            )

            c = np.array(
                quote.get("close", []),
                dtype=float
            )

            t = np.array(
                timestamps,
                dtype=np.int64
            )

            tamanho = min(
                len(t),
                len(o),
                len(h),
                len(l),
                len(c)
            )

            if tamanho < 30:
                return None

            t = t[:tamanho]
            o = o[:tamanho]
            h = h[:tamanho]
            l = l[:tamanho]
            c = c[:tamanho]

            valido = (
                np.isfinite(o)
                & np.isfinite(h)
                & np.isfinite(l)
                & np.isfinite(c)
            )

            if valido.sum() < 30:
                return None

            return {
                "time": t[valido],
                "open": o[valido],
                "high": h[valido],
                "low": l[valido],
                "close": c[valido],
            }

        except Exception:
            log.exception(
                "Erro API Yahoo."
            )

            return None

    # ========================================================
    # BINANCE
    # ========================================================

    try:

        intervalos = {
            1: "1m",
            5: "5m",
            15: "15m",
        }

        intervalo = intervalos.get(
            tf,
            "5m"
        )

        response = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={
                "symbol": ticker,
                "interval": intervalo,
                "limit": 100,
            },
            timeout=8
        )

        if response.status_code != 200:
            return None

        data = response.json()

        if not isinstance(data, list):
            return None

        if len(data) < 30:
            return None

        return {
            "time": np.array(
                [int(x[0]) for x in data],
                dtype=np.int64
            ),

            "open": np.array(
                [float(x[1]) for x in data],
                dtype=float
            ),

            "high": np.array(
                [float(x[2]) for x in data],
                dtype=float
            ),

            "low": np.array(
                [float(x[3]) for x in data],
                dtype=float
            ),

            "close": np.array(
                [float(x[4]) for x in data],
                dtype=float
            ),
        }

    except Exception:
        log.exception(
            "Erro API Binance."
        )

        return None


# ============================================================
# INDICADORES
# ============================================================

def calcular_ema(
    dados,
    periodo
):

    dados = np.asarray(
        dados,
        dtype=float
    )

    if len(dados) < periodo:
        return None

    ema = np.zeros(
        len(dados),
        dtype=float
    )

    ema[:periodo - 1] = np.nan

    ema[periodo - 1] = np.mean(
        dados[:periodo]
    )

    multiplicador = (
        2 / (periodo + 1)
    )

    for i in range(
        periodo,
        len(dados)
    ):
        ema[i] = (
            (dados[i] - ema[i - 1])
            * multiplicador
            + ema[i - 1]
        )

    return ema


def calcular_rsi(
    closes,
    periodo=14
):

    closes = np.asarray(
        closes,
        dtype=float
    )

    if len(closes) < periodo + 1:
        return None

    delta = np.diff(closes)

    ganhos = np.where(
        delta > 0,
        delta,
        0
    )

    perdas = np.where(
        delta < 0,
        -delta,
        0
    )

    media_ganho = np.mean(
        ganhos[-periodo:]
    )

    media_perda = np.mean(
        perdas[-periodo:]
    )

    if media_perda == 0:
        return 100.0

    rs = (
        media_ganho
        / media_perda
    )

    return 100 - (
        100 / (1 + rs)
    )


# ============================================================
# ESTRATÉGIAS
# ============================================================

LISTA_ESTRATEGIAS = [
    "LOGICA_DO_PRECO",
    "RSI_MACD_MA",
    "MHI1",
    "REVERSAO",
]


def analisar_estrategia(
    data,
    estrategia,
    i=-1
):

    c = data["close"]
    o = data["open"]
    h = data["high"]
    l = data["low"]

    if len(c) < 30:
        return None

    sinal = None

    # ========================================================
    # LÓGICA DO PREÇO
    # ========================================================

    if estrategia == "LOGICA_DO_PRECO":

        cor = (
            "G"
            if c[i] > o[i]
            else "R"
        )

        tamanho = abs(
            c[i] - o[i]
        )

        if tamanho <= 0:
            return None

        pavio_superior = (
            h[i]
            - max(o[i], c[i])
        )

        pavio_inferior = (
            min(o[i], c[i])
            - l[i]
        )

        tolerancia = (
            tamanho * 0.05
        )

        if (
            cor == "G"
            and pavio_inferior >= tamanho * 0.8
        ):
            sinal = "CALL"

        elif (
            cor == "R"
            and pavio_superior >= tamanho * 0.8
        ):
            sinal = "PUT"

        elif (
            cor == "G"
            and pavio_superior <= tolerancia
        ):
            sinal = "CALL"

        elif (
            cor == "R"
            and pavio_inferior <= tolerancia
        ):
            sinal = "PUT"

    # ========================================================
    # RSI + MACD + MA
    # ========================================================

    elif estrategia == "RSI_MACD_MA":

        rsi = calcular_rsi(c)

        ema12 = calcular_ema(
            c,
            12
        )

        ema26 = calcular_ema(
            c,
            26
        )

        if (
            rsi is None
            or ema12 is None
            or ema26 is None
        ):
            return None

        macd = (
            ema12 - ema26
        )

        signal_line = calcular_ema(
            macd[
                ~np.isnan(macd)
            ],
            9
        )

        if signal_line is None:
            return None

        macd_valid = macd[
            ~np.isnan(macd)
        ]

        if len(macd_valid) < 9:
            return None

        macd_atual = macd_valid[-1]
        signal_atual = signal_line[-1]

        if (
            rsi < 45
            and macd_atual > signal_atual
        ):
            sinal = "CALL"

        elif (
            rsi > 55
            and macd_atual < signal_atual
        ):
            sinal = "PUT"

    # ========================================================
    # MHI1
    # ========================================================

    elif estrategia == "MHI1":

        if len(c) < 4:
            return None

        cores = []

        for j in range(
            i - 2,
            i + 1
        ):

            if c[j] > o[j] + 1e-6:
                cores.append("G")

            elif c[j] < o[j] - 1e-6:
                cores.append("R")

            else:
                cores.append("D")

        qtd_g = cores.count("G")
        qtd_r = cores.count("R")

        if qtd_g > qtd_r:
            sinal = "PUT"

        elif qtd_r > qtd_g:
            sinal = "CALL"

    # ========================================================
    # REVERSÃO
    # ========================================================

    elif estrategia == "REVERSAO":

        janela = c[i - 20:i]

        if len(janela) < 20:
            return None

        std = np.std(janela)

        ma = np.mean(janela)

        banda_superior = (
            ma + 1.8 * std
        )

        banda_inferior = (
            ma - 1.8 * std
        )

        if c[i] <= banda_inferior:
            sinal = "CALL"

        elif c[i] >= banda_superior:
            sinal = "PUT"

    return sinal


# ============================================================
# AUTENTICAÇÃO
# ============================================================

def usuario_logado():

    email = session.get("user")

    if not email:
        return None

    email = str(
        email
    ).strip().lower()

    usuario = obter_usuario(email)

    if not usuario:
        session.clear()
        return None

    return email


def exigir_login():

    user = usuario_logado()

    if not user:
        return redirect("/login")

    return user


def exigir_admin():

    user = usuario_logado()

    if not user:
        return redirect("/login")

    if user != ADMIN_EMAIL:
        abort(403)

    return user


# ============================================================
# TEMPLATES
# ============================================================

HTML_TERMOS = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Termos de Uso</title>

<style>
body{
    background:#060913;
    color:#f1f5f9;
    font-family:Arial,sans-serif;
    padding:20px;
    line-height:1.6;
}

.card{
    max-width:650px;
    margin:auto;
    background:#0f172a;
    padding:25px;
    border-radius:16px;
    border:1px solid #00f2fe;
}

h2{
    color:#00f2fe;
}

p{
    color:#cbd5e1;
    font-size:14px;
}

.btn{
    display:block;
    text-align:center;
    padding:14px;
    background:#0072ff;
    color:white;
    text-decoration:none;
    border-radius:8px;
    margin-top:20px;
}
</style>
</head>

<body>

<div class="card">

<h2>⚖️ TERMOS DE USO E RESPONSABILIDADE</h2>

<p>
<b>1. NATUREZA DO SERVIÇO:</b>
O Vision Pro V3 é uma ferramenta de análise.
Nenhum resultado futuro é garantido.
</p>

<p>
<b>2. RISCO:</b>
Operações financeiras podem resultar em perdas.
</p>

<p>
<b>3. RESPONSABILIDADE:</b>
O usuário é responsável pelas próprias decisões.
</p>

<a class="btn" href="/login">
LI E CONCORDO
</a>

</div>

</body>
</html>
"""


HTML_LOGIN = """
<!DOCTYPE html>
<html lang="pt-BR">

<head>

<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Vision Pro V3</title>

<style>

*{
    box-sizing:border-box;
}

body{
    margin:0;
    min-height:100vh;
    display:flex;
    align-items:center;
    justify-content:center;
    background:#060913;
    color:white;
    font-family:Arial,sans-serif;
}

.card{
    width:90%;
    max-width:380px;
    padding:30px;
    border-radius:20px;
    background:#0f172a;
    border:1px solid rgba(0,242,254,.3);
    box-shadow:0 15px 50px rgba(0,0,0,.6);
}

h2{
    text-align:center;
    color:#00f2fe;
    margin-bottom:25px;
}

input{
    width:100%;
    padding:14px;
    margin:7px 0;
    border-radius:9px;
    border:1px solid #334155;
    background:#020617;
    color:white;
}

button{
    width:100%;
    padding:14px;
    margin-top:12px;
    border:0;
    border-radius:9px;
    background:#0072ff;
    color:white;
    font-weight:bold;
    cursor:pointer;
}

.error{
    background:rgba(239,68,68,.1);
    border:1px solid #ef4444;
    color:#fca5a5;
    padding:10px;
    border-radius:8px;
    margin-bottom:12px;
    font-size:13px;
}

.links{
    text-align:center;
    margin-top:20px;
}

a{
    color:#00f2fe;
    text-decoration:none;
}

</style>

</head>

<body>

<div class="card">

<h2>VISION PRO V3</h2>

{% if erro %}
<div class="error">{{ erro }}</div>
{% endif %}

<form method="POST" action="/login">

<input
type="hidden"
name="_csrf_token"
value="{{ csrf_token() }}"
>

<input
type="email"
name="email"
placeholder="Seu E-mail"
autocomplete="email"
required
>

<input
type="password"
name="password"
placeholder="Sua Senha"
autocomplete="current-password"
required
>

<button type="submit">
ACESSAR O TERMINAL
</button>

</form>

<div class="links">
<a href="/register">Criar Conta</a>
<br><br>
<a href="/termos">Termos de Uso</a>
</div>

</div>

</body>
</html>
"""


HTML_REGISTER = """
<!DOCTYPE html>
<html lang="pt-BR">

<head>

<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Criar Conta</title>

<style>

*{
box-sizing:border-box;
}

body{
margin:0;
min-height:100vh;
display:flex;
align-items:center;
justify-content:center;
background:#060913;
color:white;
font-family:Arial,sans-serif;
}

.card{
width:90%;
max-width:380px;
padding:30px;
background:#0f172a;
border-radius:20px;
border:1px solid rgba(16,185,129,.4);
}

h2{
color:#10b981;
text-align:center;
}

input{
width:100%;
padding:14px;
margin:7px 0;
border-radius:9px;
border:1px solid #334155;
background:#020617;
color:white;
}

button{
width:100%;
padding:14px;
margin-top:12px;
border:0;
border-radius:9px;
background:#059669;
color:white;
font-weight:bold;
}

.error{
color:#fca5a5;
background:rgba(239,68,68,.1);
border:1px solid #ef4444;
padding:10px;
border-radius:8px;
}

a{
display:block;
text-align:center;
margin-top:20px;
color:#00f2fe;
text-decoration:none;
}

</style>

</head>

<body>

<div class="card">

<h2>CRIAR CONTA</h2>

{% if erro %}
<div class="error">{{ erro }}</div>
{% endif %}

<form method="POST" action="/register">

<input
type="hidden"
name="_csrf_token"
value="{{ csrf_token() }}"
>

<input
type="email"
name="email"
placeholder="Novo E-mail"
autocomplete="email"
required
>

<input
type="password"
name="password"
placeholder="Nova Senha"
autocomplete="new-password"
required
>

<button>
CONCLUIR CADASTRO
</button>

</form>

<a href="/login">
Já possui uma conta?
</a>

</div>

</body>
</html>
"""


HTML_INDEX = """
<!DOCTYPE html>
<html lang="pt-BR">

<head>

<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>VISION PRO V3</title>

<style>

*{
box-sizing:border-box;
margin:0;
padding:0;
font-family:Arial,sans-serif;
}

body{
background:#060913;
color:#f1f5f9;
min-height:100vh;
padding:15px 10px;
}

.container{
width:100%;
max-width:520px;
margin:auto;
background:#0f172a;
border:1px solid rgba(0,242,254,.2);
border-radius:22px;
padding:20px;
box-shadow:0 20px 50px rgba(0,0,0,.7);
}

.header{
display:flex;
justify-content:space-between;
align-items:center;
padding-bottom:15px;
margin-bottom:15px;
border-bottom:1px solid #1e293b;
}

.brand{
font-weight:900;
color:#00f2fe;
}

.version{
font-size:10px;
background:#0b1120;
padding:5px 8px;
border-radius:8px;
color:#38ef7d;
}

.logout{
color:#ef4444;
text-decoration:none;
font-weight:bold;
font-size:12px;
}

.card{
background:#0b1120;
border:1px solid #1e293b;
border-radius:15px;
padding:15px;
margin-bottom:15px;
}

.grid{
display:grid;
grid-template-columns:repeat(3,1fr);
gap:8px;
text-align:center;
}

.label{
font-size:10px;
color:#64748b;
}

.value{
font-size:20px;
font-weight:bold;
margin-top:4px;
}

.win{
color:#10b981;
}

.loss{
color:#ef4444;
}

.blue{
color:#3b82f6;
}

.bar{
height:6px;
margin-top:15px;
background:#1e293b;
border-radius:10px;
overflow:hidden;
}

.fill{
height:100%;
background:#10b981;
width:0%;
}

.status{
padding:18px;
border-radius:15px;
border:1px solid rgba(0,242,254,.3);
background:#0b1120;
margin-bottom:15px;
text-align:center;
min-height:100px;
display:flex;
align-items:center;
justify-content:center;
}

.console{
color:#38ef7d;
font-size:13px;
}

.settings{
background:#0b1120;
border:1px solid #1e293b;
border-radius:15px;
padding:15px;
}

.section{
display:block;
color:#64748b;
font-size:10px;
font-weight:bold;
margin-bottom:9px;
text-transform:uppercase;
}

.actions{
display:flex;
gap:7px;
margin-bottom:15px;
}

button{
border:0;
cursor:pointer;
}

.action{
flex:1;
padding:12px 4px;
border-radius:9px;
color:white;
font-weight:bold;
}

.start{
background:#059669;
}

.pause{
background:#d97706;
}

.stop{
background:#dc2626;
}

.select{
width:100%;
background:#020617;
color:white;
border:1px solid #334155;
padding:11px;
border-radius:8px;
margin-bottom:12px;
}

.result-grid{
display:grid;
grid-template-columns:repeat(4,1fr);
gap:7px;
margin-bottom:15px;
}

.result{
padding:12px 3px;
border-radius:8px;
color:white;
font-weight:bold;
}

.result-win{
background:#059669;
}

.result-g1{
background:#d97706;
color:#000;
}

.result-red{
background:#dc2626;
}

.result-skip{
background:#334155;
}

.broker{
width:100%;
height:400px;
display:none;
margin-bottom:15px;
}

iframe{
width:100%;
height:100%;
border:0;
border-radius:10px;
background:#0b1120;
}

.close{
width:100%;
padding:8px;
margin-bottom:8px;
background:#334155;
color:white;
border-radius:7px;
}

.admin{
width:100%;
padding:12px;
background:#0e7490;
color:white;
border-radius:8px;
margin-top:15px;
font-weight:bold;
}

</style>

</head>

<body>

<div class="container">

<div class="header">

<div class="brand">
VISION PRO
<span class="version">V3 ULTRA</span>
</div>

<a class="logout" href="/logout">
SAIR
</a>

</div>


<div class="card">

<div class="grid">

<div>
<div class="label">WINS</div>
<div id="win-count" class="value win">0</div>
</div>

<div>
<div class="label">ASSERTIVIDADE</div>
<div id="wr-text" class="value blue">0%</div>
</div>

<div>
<div class="label">LOSS</div>
<div id="loss-count" class="value loss">0</div>
</div>

</div>

<div class="bar">
<div id="wr-fill" class="fill"></div>
</div>

</div>


<div class="card">

<div style="text-align:center;font-size:12px">

MERCADO:
<b id="mkt-badge" class="blue">
{{ modo }}
</b>

<br>

ATIVO:
<b id="current-asset" class="win">
AGUARDANDO...
</b>

</div>

</div>


<div id="broker-view-container" class="broker">

<button
class="close"
onclick="closeBrokerView()"
>
FECHAR CORRETORA
</button>

<iframe
id="brokerIframe"
src=""
sandbox="allow-forms allow-scripts allow-same-origin allow-popups"
referrerpolicy="no-referrer"
></iframe>

</div>


<div id="panel-text" class="status">
Aguardando Comando...
</div>


<div
id="result-area"
class="result-grid"
style="display:none"
>

<button
class="result result-win"
onclick="registrarResultado('win')"
>
WIN
</button>

<button
class="result result-g1"
onclick="registrarResultado('g1')"
>
G1
</button>

<button
class="result result-red"
onclick="registrarResultado('red')"
>
RED
</button>

<button
class="result result-skip"
onclick="registrarResultado('pular')"
>
PULAR
</button>

</div>


<div class="settings">

<span class="section">
CONTROLES DO ROBÔ
</span>

<div class="actions">

<button
class="action start"
onclick="sendCommand('start_bot')"
>
START
</button>

<button
class="action pause"
onclick="sendCommand('pause_bot')"
>
PAUSE
</button>

<button
class="action stop"
onclick="sendCommand('stop_bot')"
>
STOP
</button>

</div>


<span class="section">
TIPO DE MERCADO
</span>

<select
class="select"
onchange="sendCommand('mkt_' + this.value)"
>

<option value="TODOS">
Aberto — Cripto + Forex
</option>

<option value="FOREX">
Apenas Forex
</option>

<option value="CRIPTO">
Apenas Cripto
</option>

<option value="OTC">
Mercado OTC
</option>

</select>


<span class="section">
TIMEFRAME
</span>

<select
class="select"
onchange="sendCommand('tf_' + this.value)"
>

<option value="1">
M1
</option>

<option value="5" selected>
M5
</option>

<option value="15">
M15
</option>

</select>


<span class="section">
ESTRATÉGIA
</span>

<select
class="select"
onchange="sendCommand('set_est_' + this.value)"
>

<option value="TODAS">
TODAS
</option>

<option value="RSI_MACD_MA">
RSI + MACD + MA
</option>

<option value="LOGICA_DO_PRECO">
Lógica do Preço
</option>

<option value="MHI1">
MHI 1
</option>

<option value="REVERSAO">
Reversão
</option>

</select>


<span class="section">
PLATAFORMAS
</span>

<div class="actions">

<button
class="action"
style="background:#1e293b"
onclick="openBroker('https://qxbroker.com')"
>
Quotex
</button>

<button
class="action"
style="background:#1e293b"
onclick="openBroker('https://iqoption.com')"
>
IQ Option
</button>

</div>


{% if user == admin %}

<button
class="admin"
onclick="location.href='/admin_panel'"
>
PAINEL ADMINISTRATIVO
</button>

{% endif %}

</div>

</div>


<script>

let csrfToken = "{{ csrf_token() }}";


async function sendCommand(cmd){

    try{

        const response = await fetch(
            "/command/" + encodeURIComponent(cmd),
            {
                method:"POST",
                headers:{
                    "X-CSRF-Token":csrfToken
                }
            }
        );

        if(response.status === 401){
            location.href="/login";
            return;
        }

        const data = await response.json();

        if(data.redirect){
            location.href=data.redirect;
        }

    }catch(e){

        console.error(e);

    }

}


async function registrarResultado(resultado){

    try{

        const response = await fetch(
            "/resultado/" + encodeURIComponent(resultado),
            {
                method:"POST",
                headers:{
                    "X-CSRF-Token":csrfToken
                }
            }
        );

        if(response.status === 401){
            location.href="/login";
        }

    }catch(e){

        console.error(e);

    }

}


async function atualizarStatus(){

    try{

        const response = await fetch(
            "/status",
            {
                cache:"no-store"
            }
        );

        if(response.status === 401){
            location.href="/login";
            return;
        }

        const data = await response.json();

        document.getElementById(
            "panel-text"
        ).innerHTML = data.html || "Aguardando...";


        document.getElementById(
            "win-count"
        ).innerText = data.wins || 0;


        document.getElementById(
            "loss-count"
        ).innerText = data.reds || 0;


        document.getElementById(
            "wr-text"
        ).innerText =
            (data.winrate || 0) + "%";


        document.getElementById(
            "wr-fill"
        ).style.width =
            Math.min(
                Math.max(
                    Number(data.winrate || 0),
                    0
                ),
                100
            ) + "%";


        document.getElementById(
            "result-area"
        ).style.display =
            data.aguardando
            ? "grid"
            : "none";


        document.getElementById(
            "mkt-badge"
        ).innerText =
            data.mercado || "TODOS";


        document.getElementById(
            "current-asset"
        ).innerText =
            data.rodando
            ? (
                data.ativo_atual
                || "VARRRENDO..."
              )
            : "SISTEMA PAUSADO";


    }catch(e){

        console.error(e);

    }

}


function openBroker(url){

    const container =
        document.getElementById(
            "broker-view-container"
        );

    const iframe =
        document.getElementById(
            "brokerIframe"
        );

    iframe.src = url;

    container.style.display = "block";

}


function closeBrokerView(){

    const container =
        document.getElementById(
            "broker-view-container"
        );

    const iframe =
        document.getElementById(
            "brokerIframe"
        );

    iframe.src = "";

    container.style.display = "none";

}


atualizarStatus();

setInterval(
    atualizarStatus,
    1500
);

</script>

</body>
</html>
"""


# ============================================================
# PAINEL ADMIN
# ============================================================

HTML_ADM = """
<!DOCTYPE html>
<html lang="pt-BR">

<head>

<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Administração</title>

<style>

body{
background:#060913;
color:white;
font-family:Arial,sans-serif;
padding:15px;
}

.container{
max-width:800px;
margin:auto;
}

.card{
background:#0f172a;
border:1px solid #1e293b;
border-radius:12px;
padding:15px;
margin-bottom:12px;
}

input{
width:100%;
padding:10px;
margin:5px 0;
background:#020617;
border:1px solid #334155;
color:white;
border-radius:7px;
}

button,
a{
display:inline-block;
padding:9px 12px;
border:0;
border-radius:7px;
margin:4px;
text-decoration:none;
font-weight:bold;
cursor:pointer;
}

.blue{
background:#2563eb;
color:white;
}

.green{
background:#059669;
color:white;
}

.orange{
background:#d97706;
color:white;
}

.red{
background:#dc2626;
color:white;
}

.back{
color:#00f2fe;
}

.online{
color:#10b981;
}

.offline{
color:#64748b;
}

</style>

<script>

function toggle(id){

    const el =
        document.getElementById(id);

    el.style.display =
        el.style.display === "block"
        ? "none"
        : "block";

}

</script>

</head>

<body>

<div class="container">

<h2>
🛡️ GESTÃO DE USUÁRIOS
</h2>

<p>
Online:
<b>{{ online_count }}</b>
</p>

<a class="back" href="/">
⬅ Voltar
</a>

{% for email, info in lista.items() %}

<div class="card">

<div
onclick="toggle('details-{{ loop.index }}')"
style="cursor:pointer"
>

<b>{{ email }}</b>

{% if email in online_list %}

<span class="online">
● ONLINE
</span>

{% else %}

<span class="offline">
● OFFLINE
</span>

{% endif %}

</div>


<div
id="details-{{ loop.index }}"
style="display:none;margin-top:15px"
>

<p>
Wins:
<b>{{ info.wins }}</b>
</p>

<p>
Reds:
<b>{{ info.reds }}</b>
</p>

<p>
Assertividade:
<b>{{ info.winrate }}%</b>
</p>

<p>
IPs:
<b>{{ info.ips_Formatados }}</b>
</p>


<form
method="POST"
action="/adm/editar"
>

<input
type="hidden"
name="_csrf_token"
value="{{ csrf_token() }}"
>

<input
type="hidden"
name="email_original"
value="{{ email }}"
>

<input
type="email"
name="novo_email"
value="{{ email }}"
required
>

<input
type="password"
name="nova_senha"
placeholder="Nova senha — opcional"
>

<button class="blue">
SALVAR
</button>

</form>


<form
method="POST"
action="/adm/renovar"
style="display:inline"
>

<input
type="hidden"
name="_csrf_token"
value="{{ csrf_token() }}"
>

<input
type="hidden"
name="email"
value="{{ email }}"
>

<button class="green">
+30 DIAS
</button>

</form>


<form
method="POST"
action="/adm/liberar_ip"
style="display:inline"
>

<input
type="hidden"
name="_csrf_token"
value="{{ csrf_token() }}"
>

<input
type="hidden"
name="email"
value="{{ email }}"
>

<button class="orange">
LIBERAR IP
</button>

</form>


{% if email != admin %}

<form
method="POST"
action="/adm/excluir"
style="display:inline"
onsubmit="return confirm('Excluir este usuário?')"
>

<input
type="hidden"
name="_csrf_token"
value="{{ csrf_token() }}"
>

<input
type="hidden"
name="email"
value="{{ email }}"
>

<button class="red">
EXCLUIR
</button>

</form>

{% endif %}

</div>

</div>

{% endfor %}

</div>

</body>
</html>
"""


# ============================================================
# ROTAS
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok"
    })


@app.route("/termos")
def termos():

    return render_template_string(
        HTML_TERMOS
    )


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        validar_csrf()

        ip = get_client_ip()

        if not rate_limit_login(ip):

            return render_template_string(
                HTML_LOGIN,
                erro=(
                    "Muitas tentativas. "
                    "Aguarde alguns minutos."
                )
            ), 429

        email = (
            request.form
            .get("email", "")
            .strip()
            .lower()
        )

        password = request.form.get(
            "password",
            ""
        )

        if (
            not validar_email(email)
            or not password
        ):

            return render_template_string(
                HTML_LOGIN,
                erro="E-mail ou senha inválidos."
            ), 400


        usuario = obter_usuario(email)

        if not usuario:

            return render_template_string(
                HTML_LOGIN,
                erro="E-mail ou senha inválidos."
            ), 401


        try:

            senha_ok = check_password_hash(
                usuario["senha"],
                password
            )

        except Exception:

            senha_ok = False


        if not senha_ok:

            return render_template_string(
                HTML_LOGIN,
                erro="E-mail ou senha inválidos."
            ), 401


        assinatura_ok, _ = verificar_assinatura(
            email
        )

        if not assinatura_ok:

            return render_template_string(
                HTML_LOGIN,
                erro="Sua assinatura expirou."
            ), 403


        if not verificar_ip_usuario(email):

            return render_template_string(
                HTML_LOGIN,
                erro=(
                    "Este dispositivo não está "
                    "autorizado para esta conta."
                )
            ), 403


        if not registrar_ip_usuario(email):

            return render_template_string(
                HTML_LOGIN,
                erro=(
                    "Limite de dispositivos atingido."
                )
            ), 403


        session.clear()

        session.permanent = True

        session["user"] = email

        session["_csrf_token"] = secrets.token_urlsafe(
            32
        )

        return redirect("/")


    return render_template_string(
        HTML_LOGIN
    )


@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        validar_csrf()

        email = (
            request.form
            .get("email", "")
            .strip()
            .lower()
        )

        password = request.form.get(
            "password",
            ""
        )


        if not validar_email(email):

            return render_template_string(
                HTML_REGISTER,
                erro="Informe um e-mail válido."
            ), 400


        if not validar_senha(password):

            return render_template_string(
                HTML_REGISTER,
                erro=(
                    "A senha deve possuir "
                    "entre 8 e 128 caracteres."
                )
            ), 400


        if email == ADMIN_EMAIL:

            return render_template_string(
                HTML_REGISTER,
                erro=(
                    "Esta conta é reservada "
                    "à administração."
                )
            ), 403


        if obter_usuario(email):

            return render_template_string(
                HTML_REGISTER,
                erro="Este e-mail já está cadastrado."
            ), 409


        try:

            salvar_usuario(
                email,
                password,
                ip_inicial=get_client_ip()
            )

        except Exception:

            log.exception(
                "Erro criando usuário."
            )

            return render_template_string(
                HTML_REGISTER,
                erro="Não foi possível criar a conta."
            ), 500


        session.clear()

        session.permanent = True

        session["user"] = email

        session["_csrf_token"] = secrets.token_urlsafe(
            32
        )

        return redirect("/")


    return render_template_string(
        HTML_REGISTER
    )


@app.route("/logout")
def logout():

    user = session.get("user")

    if user:
        with ESTADOS_LOCK:
            estado = ESTADOS.get(user)

            if estado:
                estado["bot_iniciado"] = False
                estado["bot_pausado"] = True
                estado["aguardando_resultado"] = False

    session.clear()

    return redirect("/login")


# ============================================================
# PAINEL PRINCIPAL
# ============================================================

@app.route("/")
def index():

    user = usuario_logado()

    if not user:
        return redirect("/login")

    assinatura_ok, _ = verificar_assinatura(
        user
    )

    if not assinatura_ok:
        session.clear()

        return redirect("/login")


    estado = obter_estado(user)

    return render_template_string(
        HTML_INDEX,
        modo=estado["mercado"],
        tf=estado["timeframe"],
        estrat=estado["estrategia"],
        user=user,
        admin=ADMIN_EMAIL
    )


# ============================================================
# STATUS
# ============================================================

@app.route("/status")
def status():

    user = usuario_logado()

    if not user:
        return jsonify({
            "error": "unauthorized"
        }), 401


    estado = obter_estado(user)

    usuario = obter_usuario(user)

    if not usuario:
        session.clear()

        return jsonify({
            "error": "unauthorized"
        }), 401


    html = (
        estado["sinal_display"]
        if (
            estado["aguardando_resultado"]
            and estado["sinal_display"]
        )
        else estado["ultimo_sinal"]
    )


    return jsonify({

        "html": html,

        "aguardando":
            estado["aguardando_resultado"],

        "wins":
            usuario.get("wins", 0),

        "reds":
            usuario.get("reds", 0),

        "winrate":
            usuario.get("winrate", 0.0),

        "ativo_atual":
            estado["ativo_atual"],

        "mercado":
            estado["mercado"],

        "rodando":
            (
                estado["bot_iniciado"]
                and not estado["bot_pausado"]
            ),

    })


# ============================================================
# COMANDOS
# ============================================================

@app.route(
    "/command/<cmd>",
    methods=["POST"]
)
def command(cmd):

    user = usuario_logado()

    if not user:
        return jsonify({
            "error": "unauthorized"
        }), 401


    validar_csrf_header()


    estado = obter_estado(user)


    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    if cmd == "start_bot":

        estado["bot_iniciado"] = True

        estado["bot_pausado"] = False

        estado["aguardando_resultado"] = False

        estado["sinal_display"] = None

        estado["ativo_atual"] = (
            "INICIANDO VARREDURA..."
        )

        estado["ultimo_sinal"] = (
            "<div class='console'>"
            "⚡ <b>VARREDURA INICIADA</b>"
            "</div>"
        )

        return jsonify({
            "ok": True
        })


    # --------------------------------------------------------
    # PAUSE
    # --------------------------------------------------------

    if cmd == "pause_bot":

        estado["bot_pausado"] = (
            not estado["bot_pausado"]
        )

        if estado["bot_pausado"]:

            estado["ultimo_sinal"] = (
                "<div class='console' "
                "style='color:#f59e0b'>"
                "⏸ SISTEMA PAUSADO"
                "</div>"
            )

        else:

            estado["ultimo_sinal"] = (
                "<div class='console'>"
                "🔍 ANALISANDO: "
                f"<b>{estado['ativo_atual']}</b>"
                "</div>"
            )

        return jsonify({
            "ok": True
        })


    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    if cmd == "stop_bot":

        estado["bot_iniciado"] = False

        estado["bot_pausado"] = True

        estado["aguardando_resultado"] = False

        estado["sinal_display"] = None

        estado["ativo_atual"] = (
            "DESCONECTADO"
        )

        estado["ultimo_sinal"] = (
            "Aguardando Comando..."
        )

        return jsonify({
            "ok": True
        })


    # --------------------------------------------------------
    # TIMEFRAME
    # --------------------------------------------------------

    if cmd.startswith("tf_"):

        try:

            valor = int(
                cmd.split("_", 1)[1]
            )

        except Exception:

            return jsonify({
                "error": "Timeframe inválido."
            }), 400


        if valor not in (1, 5, 15):

            return jsonify({
                "error": "Timeframe inválido."
            }), 400


        estado["timeframe"] = valor

        return jsonify({
            "ok": True
        })


    # --------------------------------------------------------
    # MERCADO
    # --------------------------------------------------------

    if cmd.startswith("mkt_"):

        valor = cmd.split(
            "_",
            1
        )[1].upper()

        if valor not in (
            "TODOS",
            "FOREX",
            "CRIPTO",
            "OTC"
        ):

            return jsonify({
                "error": "Mercado inválido."
            }), 400


        estado["mercado"] = valor

        return jsonify({
            "ok": True
        })


    # --------------------------------------------------------
    # ESTRATÉGIA
    # --------------------------------------------------------

    if cmd.startswith("set_est_"):

        valor = cmd.replace(
            "set_est_",
            "",
            1
        )

        if valor != "TODAS" and valor not in LISTA_ESTRATEGIAS:

            return jsonify({
                "error": "Estratégia inválida."
            }), 400


        estado["estrategia"] = valor

        return jsonify({
            "ok": True
        })


    return jsonify({
        "error": "Comando desconhecido."
    }), 404


# ============================================================
# CSRF POR HEADER
# ============================================================

def validar_csrf_header():

    session_token = session.get(
        "_csrf_token"
    )

    recebido = request.headers.get(
        "X-CSRF-Token",
        ""
    )

    if (
        not session_token
        or not recebido
        or not secrets.compare_digest(
            session_token,
            recebido
        )
    ):
        abort(
            400,
            description="CSRF inválido."
        )


# ============================================================
# RESULTADOS
# ============================================================

@app.route(
    "/resultado/<res>",
    methods=["POST"]
)
def resultado(res):

    user = usuario_logado()

    if not user:
        return jsonify({
            "error": "unauthorized"
        }), 401


    validar_csrf_header()


    if res not in (
        "win",
        "g1",
        "red",
        "pular"
    ):
        abort(400)


    estado = obter_estado(user)


    if not estado["aguardando_resultado"]:

        return jsonify({
            "ok": False,
            "message": "Nenhum sinal aguardando resultado."
        })


    if res in ("win", "g1"):

        atualizar_estatisticas_usuario(
            user,
            "win"
        )

        registrar_sinal_bd(
            user,
            estado["ultimo_ativo_sinal"]
            or "desconhecido",
            res
        )

    elif res == "red":

        atualizar_estatisticas_usuario(
            user,
            "red"
        )

        registrar_sinal_bd(
            user,
            estado["ultimo_ativo_sinal"]
            or "desconhecido",
            "red"
        )


    estado["aguardando_resultado"] = False

    estado["sinal_display"] = None

    estado["ultimo_resultado"] = res

    estado["ultimo_sinal"] = (
        "<div class='console'>"
        "🔍 RETOMANDO VARREDURA..."
        "</div>"
    )


    return jsonify({
        "ok": True
    })


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin_panel")
def admin_panel():

    admin = exigir_admin()

    if not isinstance(admin, str):
        return admin


    usuarios = carregar_usuarios()

    agora = time.time()

    online_list = [
        email
        for email, timestamp
        in ONLINE.items()
        if agora - timestamp < 60
    ]


    return render_template_string(
        HTML_ADM,
        lista=usuarios,
        online_list=online_list,
        online_count=len(online_list),
        admin=ADMIN_EMAIL
    )


# ============================================================
# ONLINE
# ============================================================

ONLINE = {}
ONLINE_LOCK = threading.Lock()


@app.before_request
def registrar_online():

    user = session.get("user")

    if user:

        with ONLINE_LOCK:
            ONLINE[user] = time.time()


# ============================================================
# ADMIN — EDITAR
# ============================================================

@app.route(
    "/adm/editar",
    methods=["POST"]
)
def admin_editar():

    admin = exigir_admin()

    if not isinstance(admin, str):
        return admin


    validar_csrf()


    email_original = (
        request.form
        .get("email_original", "")
        .strip()
        .lower()
    )

    novo_email = (
        request.form
        .get("novo_email", "")
        .strip()
        .lower()
    )

    nova_senha = request.form.get(
        "nova_senha",
        ""
    )


    if not validar_email(
        email_original
    ):
        abort(400)


    if not validar_email(
        novo_email
    ):
        abort(400)


    usuario = obter_usuario(
        email_original
    )

    if not usuario:
        abort(404)


    conn = None
    cur = None

    try:

        conn = get_db_connection()

        cur = conn.cursor()

        if nova_senha:

            if not validar_senha(
                nova_senha
            ):
                abort(
                    400,
                    description="Senha inválida."
                )

            senha_hash = generate_password_hash(
                nova_senha,
                method="scrypt"
            )

            cur.execute("""
                UPDATE usuarios
                SET
                    email = %s,
                    senha = %s
                WHERE email = %s;
            """, (
                novo_email,
                senha_hash,
                email_original
            ))

        else:

            cur.execute("""
                UPDATE usuarios
                SET email = %s
                WHERE email = %s;
            """, (
                novo_email,
                email_original
            ))


        conn.commit()


    except Exception:

        if conn:
            conn.rollback()

        log.exception(
            "Erro editando usuário."
        )

        return redirect(
            "/admin_panel"
        )

    finally:

        if cur:
            cur.close()

        if conn:
            conn.close()


    return redirect(
        "/admin_panel"
    )


# ============================================================
# ADMIN — RENOVAR
# ============================================================

@app.route(
    "/adm/renovar",
    methods=["POST"]
)
def admin_renovar():

    admin = exigir_admin()

    if not isinstance(admin, str):
        return admin


    validar_csrf()


    email = (
        request.form
        .get("email", "")
        .strip()
        .lower()
    )


    if not validar_email(email):
        abort(400)


    if not obter_usuario(email):
        abort(404)


    nova_data = (
        agora_brasilia()
        .strftime("%Y-%m-%d")
    )


    conn = None
    cur = None

    try:

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            UPDATE usuarios
            SET criado_em = %s
            WHERE email = %s;
        """, (
            nova_data,
            email
        ))

        conn.commit()

    except Exception:

        if conn:
            conn.rollback()

        log.exception(
            "Erro renovando usuário."
        )

    finally:

        if cur:
            cur.close()

        if conn:
            conn.close()


    return redirect(
        "/admin_panel"
    )


# ============================================================
# ADMIN — LIBERAR IP
# ============================================================

@app.route(
    "/adm/liberar_ip",
    methods=["POST"]
)
def admin_liberar_ip():

    admin = exigir_admin()

    if not isinstance(admin, str):
        return admin


    validar_csrf()


    email = (
        request.form
        .get("email", "")
        .strip()
        .lower()
    )


    if not validar_email(email):
        abort(400)


    conn = None
    cur = None

    try:

        conn = get_db_connection()

        cur = conn.cursor()

        cur.execute("""
            UPDATE usuarios
            SET ips_autorizados = '[]'
            WHERE email = %s;
        """, (
            email,
        ))

        conn.commit()

    except Exception:

        if conn:
            conn.rollback()

        log.exception(
            "Erro liberando IP."
        )

    finally:

        if cur:
            cur.close()

        if conn:
            conn.close()


    return redirect(
        "/admin_panel"
    )


# ============================================================
# ADMIN — EXCLUIR
# ============================================================

@app.route(
    "/adm/excluir",
    methods=["POST"]
)
def admin_excluir():

    admin = exigir_admin()

    if not isinstance(admin, str):
        return admin


    validar_csrf()


    email = (
        request.form
        .get("email", "")
        .strip()
        .lower()
    )


    if email == ADMIN_EMAIL:
        abort(403)


    if not validar_email(email):
        abort(400)


    conn = None
    cur = None

    try:

        conn = get_db_connection()

        cur = conn.cursor()

        cur.execute("""
            DELETE FROM historico_sinais
            WHERE user_email = %s;
        """, (
            email,
        ))

        cur.execute("""
            DELETE FROM usuarios
            WHERE email = %s;
        """, (
            email,
        ))

        conn.commit()


        limpar_estado(email)

        with ONLINE_LOCK:
            ONLINE.pop(email, None)


    except Exception:

        if conn:
            conn.rollback()

        log.exception(
            "Erro excluindo usuário."
        )

    finally:

        if cur:
            cur.close()

        if conn:
            conn.close()


    return redirect(
        "/admin_panel"
    )


# ============================================================
# LOOP DO BOT
# ============================================================

BOT_GLOBAL_ATIVO = True


def gerar_sinal_para_usuario(
    email,
    estado
):

    mercado = estado["mercado"]

    timeframe = estado["timeframe"]

    estrategia = estado["estrategia"]


    if mercado == "OTC":

        estado["ultimo_sinal"] = (
            "<div class='console' "
            "style='color:#f59e0b'>"
            "⚠️ MERCADO OTC"
            "<br>"
            "Análise automática indisponível."
            "</div>"
        )

        return False


    if mercado == "TODOS":

        ativos = (
            ATIVOS_BASE["FOREX"]
            + ATIVOS_BASE["CRIPTO"]
        )

    else:

        ativos = ATIVOS_BASE.get(
            mercado,
            []
        )


    for ativo in ativos:

        if not (
            estado["bot_iniciado"]
            and not estado["bot_pausado"]
            and not estado[
                "aguardando_resultado"
            ]
        ):
            return False


        estado["ativo_atual"] = ativo


        ticker = MAPA_TICKERS.get(
            ativo
        )


        mercado_atual = (
            "CRIPTO"
            if ativo in ATIVOS_BASE["CRIPTO"]
            else "FOREX"
        )


        estado["ultimo_sinal"] = (
            "<div class='console'>"
            "🔍 ANALISANDO: "
            f"<b>{ativo}</b>"
            f" — M{timeframe}"
            "</div>"
        )


        data = get_data_v2(
            ticker,
            timeframe,
            mercado_atual
        )


        if not data:
            continue


        sinal = None

        estrategia_encontrada = estrategia


        if estrategia == "TODAS":

            sinais = []

            for est in LISTA_ESTRATEGIAS:

                resultado = analisar_estrategia(
                    data,
                    est
                )

                if resultado:
                    sinais.append(
                        (est, resultado)
                    )


            if sinais:

                # Só aceita quando há pelo menos
                # duas estratégias concordando.
                contagem_call = sum(
                    1
                    for _, s in sinais
                    if s == "CALL"
                )

                contagem_put = sum(
                    1
                    for _, s in sinais
                    if s == "PUT"
                )


                if contagem_call >= 2:

                    sinal = "CALL"

                    estrategia_encontrada = (
                        "CONFLUÊNCIA"
                    )

                elif contagem_put >= 2:

                    sinal = "PUT"

                    estrategia_encontrada = (
                        "CONFLUÊNCIA"
                    )

        else:

            sinal = analisar_estrategia(
                data,
                estrategia
            )


        if not sinal:
            continue


        agora = agora_brasilia()


        minutos_passados = (
            agora.minute
            % timeframe
        )


        segundos_passados = (
            minutos_passados * 60
            + agora.second
        )


        segundos_restantes = (
            timeframe * 60
            - segundos_passados
        )


        proxima_entrada = (
            agora
            + timedelta(
                seconds=segundos_restantes
            )
        )


        saida = (
            proxima_entrada
            + timedelta(
                minutes=timeframe
            )
        )


        entrada_str = (
            proxima_entrada
            .strftime("%H:%M")
        )


        saida_str = (
            saida
            .strftime("%H:%M")
        )


        estado["ultimo_sinal"] = (
            "<div style='text-align:center'>"
            "⚠️ <b>PREPARE O ATIVO</b>"
            "<br><br>"
            f"<b>{ativo}</b>"
            "<br>"
            f"Entrada: <b>{entrada_str}</b>"
            "<br>"
            f"M{timeframe}"
            "<br><br>"
            "<small>"
            "Aguardando confirmação..."
            "</small>"
            "</div>"
        )


        # Aguarda a próxima vela.
        while (
            agora_brasilia()
            < proxima_entrada
        ):

            if not (
                estado["bot_iniciado"]
                and not estado["bot_pausado"]
            ):
                return False

            if estado[
                "aguardando_resultado"
            ]:
                return False

            time.sleep(0.5)


        direcao_cor = (
            "#10b981"
            if sinal == "CALL"
            else "#ef4444"
        )


        estado["sinal_display"] = (
            "<div>"
            "<h3 style='color:#00f2fe'>"
            "🎯 SINAL CONFIRMADO"
            "</h3>"
            "<br>"
            f"<b>ATIVO:</b> {ativo}"
            "<br>"
            f"<b>DIREÇÃO:</b> "
            f"<span style='color:{direcao_cor}'>"
            f"{sinal}"
            "</span>"
            "<br>"
            f"<b>TIMEFRAME:</b> M{timeframe}"
            "<br>"
            f"<b>EXPIRAÇÃO:</b> {saida_str}"
            "<br>"
            f"<small>"
            f"Estratégia: {estrategia_encontrada}"
            "</small>"
            "</div>"
        )


        estado["ultimo_sinal"] = (
            estado["sinal_display"]
        )


        estado["ultimo_ativo_sinal"] = (
            f"{ativo} | "
            f"{sinal} | "
            f"M{timeframe}"
        )


        estado["entrada"] = entrada_str

        estado["saida"] = saida_str

        estado["aguardando_resultado"] = True


        registrar_sinal_bd(
            email,
            estado["ultimo_ativo_sinal"]
        )


        return True


    return False


def bot_loop():

    while BOT_GLOBAL_ATIVO:

        try:

            usuarios = carregar_usuarios()

            for email in usuarios:

                estado = obter_estado(email)


                if not (
                    estado["bot_iniciado"]
                    and not estado["bot_pausado"]
                ):
                    continue


                if estado[
                    "aguardando_resultado"
                ]:
                    continue


                try:

                    gerar_sinal_para_usuario(
                        email,
                        estado
                    )

                except Exception:

                    log.exception(
                        "Erro no bot do usuário %s",
                        email
                    )


                time.sleep(0.2)


            time.sleep(1)


        except Exception:

            log.exception(
                "Erro geral no loop do bot."
            )

            time.sleep(5)


# ============================================================
# INICIAR THREAD DO BOT
# ============================================================

thread_bot = threading.Thread(
    target=bot_loop,
    daemon=True,
    name="VisionBot"
)

thread_bot.start()


# ============================================================
# EXECUÇÃO
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True
    )
