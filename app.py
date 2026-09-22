import requests
import time
import math
import asyncio
import pytz
import threading
import json
import sys
import random
import os
import logging
import gc
import hmac
import numpy as np
import re
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify, session, redirect, abort, Response
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor

# ================= AJUSTE DE FUSO HORÁRIO (SÃO PAULO / BRASÍLIA) =================
FUSO_SP = pytz.timezone('America/Sao_Paulo')

def agora_brasilia():
    return datetime.now(FUSO_SP)

# ================= CONFIGURAÇÕES DE AMBIENTE E BOT TELEGRAM =================
# NENHUMA credencial real fica armazenada no código-fonte.
TOKEN_TELEGRAM = os.getenv("TOKEN_TELEGRAM", "").strip()
CHAT_ID_TELEGRAM = os.getenv("CHAT_ID_TELEGRAM", "").strip()
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
FLASK_SECRET = os.getenv("FLASK_SECRET", "").strip()
TWELVE_DATA_API_KEY = (
    os.getenv("TWELVE_DATA_API_KEY", "").strip()
    or os.getenv("TWELVEDATA_API_KEY", "").strip()
)

def obter_twelve_data_api_key():
    """Lê a chave da Twelve Data diretamente do ambiente a cada requisição.
    Aceita os dois nomes usados no Render: TWELVE_DATA_API_KEY e TWELVEDATA_API_KEY.
    """
    return (
        os.getenv("TWELVE_DATA_API_KEY", "").strip()
        or os.getenv("TWELVEDATA_API_KEY", "").strip()
        or TWELVE_DATA_API_KEY
    )

# ================= FONTE OTC DA QUOTEX =================
# Credenciais somente via variáveis de ambiente do Render.
# Esta integração é somente de leitura: o bot não executa ordens na Quotex.
QUOTEX_EMAIL = os.getenv("QUOTEX_EMAIL", "").strip()
QUOTEX_PASSWORD = os.getenv("QUOTEX_PASSWORD", "")
QUOTEX_SSID = os.getenv("QUOTEX_SSID", "").strip()
QUOTEX_HOST = os.getenv("QUOTEX_HOST", "").strip().lower()
QUOTEX_HOSTS = os.getenv("QUOTEX_HOSTS", "").strip()

DB_URL = os.getenv("DB_URL") or os.getenv("DATABASE_URL", "").strip()

if not FLASK_SECRET:
    raise RuntimeError("FLASK_SECRET não configurada. Defina uma chave aleatória forte nas variáveis de ambiente.")
if not ADMIN_EMAIL:
    raise RuntimeError("ADMIN_EMAIL não configurado. Defina o e-mail do administrador nas variáveis de ambiente.")

def get_db_connection():
    if not DB_URL:
        raise ValueError("A variável de ambiente DB_URL (ou DATABASE_URL) precisa estar configurada.")
    return psycopg2.connect(DB_URL)

USUARIOS_ONLINE = {}
DADOS_USUARIOS = {}  # Estrutura isolada por e-mail de usuário

def get_user_state(email):
    """Garante que cada usuário possui seu próprio estado independente no painel."""
    if not email:
        return None
    email_clean = email.strip().lower()
    if email_clean not in DADOS_USUARIOS:
        DADOS_USUARIOS[email_clean] = {
            "timeframe": 5,
            "tipo_mercado": "TODOS",
            "ativo_selecionado": "TODOS",
            "ativos_selecionados": ["TODOS"],
            "fonte_dados": "AGUARDANDO...",
            "estrategia": "TODAS",
            "bot_iniciado": False,
            "bot_pausado": True,
            "aguardando_confirmacao": False,
            "sinal_permanente": None,
            "ultimo_sinal": "Aguardando Comando...",
            "ativo_atual": "AGUARDANDO...",
            "inicio_varredura": 0,
            "sinais_enviados": {},
            "alerta_ativo": None,  # Guarda informações do alerta ativo no ciclo
            "timer_confirmacao": None,  # Timer independente para não depender da varredura
            "notificacao": None,
            "notificacao_ultima_hora": 0.0,
            "sinal_confirmado_dados": None,
            "ultimo_resumo_sessao": None,
            "telegram_ativo": False,
            "candle_inicio": None,
            "candle_decorrido": 0,
            "candle_restante": 0
        }
    return DADOS_USUARIOS[email_clean]

def get_client_ip():
    # Em produção, o proxy confiável pode fornecer X-Forwarded-For.
    # Não aceitamos esse cabeçalho cegamente de clientes externos.
    if os.getenv("TRUST_PROXY", "true").lower() == "true":
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"

# ================= ENVIO E DELEÇÃO TELEGRAM =================
def _telegram_request(method, payload=None, timeout=12):
    """Executa uma chamada à API do Telegram e devolve resposta estruturada."""
    if not TOKEN_TELEGRAM or not CHAT_ID_TELEGRAM:
        return False, None, "TOKEN_TELEGRAM ou CHAT_ID_TELEGRAM não configurado no ambiente."

    token = TOKEN_TELEGRAM.strip()
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        res = requests.post(url, json=payload or {}, timeout=timeout)
        try:
            data = res.json()
        except ValueError:
            return False, None, f"Telegram retornou HTTP {res.status_code} sem JSON válido."

        if data.get("ok"):
            return True, data.get("result"), None

        descricao = data.get("description") or f"Erro HTTP {res.status_code}"
        codigo = data.get("error_code", res.status_code)
        return False, None, f"Telegram HTTP {codigo}: {descricao}"
    except requests.exceptions.Timeout:
        return False, None, "Tempo esgotado ao conectar com a API do Telegram."
    except requests.exceptions.RequestException as e:
        return False, None, f"Erro de conexão com o Telegram: {e}"
    except Exception as e:
        return False, None, f"Erro inesperado no Telegram: {e}"


def diagnosticar_telegram():
    """Valida token, chat e permissões sem expor o token nos logs."""
    if not TOKEN_TELEGRAM:
        return False, "TOKEN_TELEGRAM não está configurado no Render."
    if not CHAT_ID_TELEGRAM or not str(CHAT_ID_TELEGRAM).strip():
        return False, "CHAT_ID_TELEGRAM não está configurado no Render."

    ok_me, bot_info, err_me = _telegram_request("getMe", {}, timeout=10)
    if not ok_me:
        return False, f"Token do bot inválido ou inacessível: {err_me}"

    ok_chat, chat_info, err_chat = _telegram_request(
        "getChat", {"chat_id": str(CHAT_ID_TELEGRAM).strip()}, timeout=10
    )
    if not ok_chat:
        return False, f"CHAT_ID_TELEGRAM inválido ou chat inacessível: {err_chat}"

    bot_name = bot_info.get("username", "bot") if isinstance(bot_info, dict) else "bot"
    chat_title = chat_info.get("title") or chat_info.get("username") or chat_info.get("first_name") or str(CHAT_ID_TELEGRAM)
    return True, f"Bot @{bot_name} conectado ao chat {chat_title}."


def enviar_telegram(mensagem, auto_delete=None, user_solicitante=None, tentativas=3):
    """Envia mensagem ao Telegram somente quando autorizado pelo ADM."""
    if user_solicitante != ADMIN_EMAIL or not telegram_envio_ativo():
        print("ℹ️ Telegram: envio bloqueado (somente ADM + Telegram ativo).")
        return None
    if not TOKEN_TELEGRAM or not CHAT_ID_TELEGRAM:
        print("❌ Telegram não configurado: TOKEN_TELEGRAM ou CHAT_ID_TELEGRAM ausente.")
        return None

    chat_id = str(CHAT_ID_TELEGRAM).strip()
    texto_html = str(mensagem)
    ultimo_erro = "erro desconhecido"

    for tentativa in range(1, max(1, tentativas) + 1):
        ok, result, erro = _telegram_request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": texto_html,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=15
        )

        if ok and isinstance(result, dict) and result.get("message_id"):
            msg_id = result["message_id"]
            print(f"✅ Telegram: mensagem enviada com sucesso (ID {msg_id}).")
            if auto_delete:
                threading.Thread(
                    target=deletar_mensagem_atrasada,
                    args=(msg_id, auto_delete),
                    daemon=True
                ).start()
            return msg_id

        ultimo_erro = erro or "Telegram não retornou message_id."

        # Se o problema for HTML inválido, tenta texto puro imediatamente.
        if tentativa == 1 and (erro and ("parse" in erro.lower() or "entities" in erro.lower() or "html" in erro.lower())):
            texto_limpo = re.sub(r"<[^<]+?>", "", texto_html)
            ok_plain, result_plain, erro_plain = _telegram_request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": texto_limpo,
                    "disable_web_page_preview": True
                },
                timeout=15
            )
            if ok_plain and isinstance(result_plain, dict) and result_plain.get("message_id"):
                msg_id = result_plain["message_id"]
                print(f"✅ Telegram: mensagem enviada em texto puro (ID {msg_id}).")
                if auto_delete:
                    threading.Thread(target=deletar_mensagem_atrasada, args=(msg_id, auto_delete), daemon=True).start()
                return msg_id
            ultimo_erro = erro_plain or ultimo_erro

        if tentativa < max(1, tentativas):
            time.sleep(1.2 * tentativa)

    print(f"❌ Telegram: falha definitiva após {max(1, tentativas)} tentativa(s): {ultimo_erro}")
    return None

def deletar_mensagem_telegram(msg_id):
    """Remove uma mensagem do canal e registra o resultado nos logs."""
    if not TOKEN_TELEGRAM or not CHAT_ID_TELEGRAM or not msg_id:
        return False
    try:
        ok, _, erro = _telegram_request(
            "deleteMessage",
            {
                "chat_id": str(CHAT_ID_TELEGRAM).strip(),
                "message_id": int(msg_id)
            },
            timeout=8
        )
        if ok:
            print(f"🗑️ Telegram: mensagem {msg_id} apagada com sucesso.")
            return True
        print(f"⚠️ Telegram: não foi possível apagar a mensagem {msg_id}: {erro}")
    except Exception as e:
        print(f"⚠️ Erro ao deletar mensagem Telegram {msg_id}: {e}")
    return False

def deletar_mensagem_atrasada(msg_id, delay):
    if delay > 0: time.sleep(delay)
    deletar_mensagem_telegram(msg_id)

# ================= SERVIDOR FLASK =================
app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true",
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# ================= SEGURANÇA HTTP / CSRF =================
def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = os.urandom(32).hex()
        session["csrf_token"] = token
    return token

@app.template_global("csrf_token")
def csrf_token_template():
    return get_csrf_token()

@app.before_request
def protect_state_changing_requests():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        expected = session.get("csrf_token")
        if not expected or not token or not hmac.compare_digest(str(token), str(expected)):
            return jsonify({"ok": False, "error": "CSRF inválido ou sessão expirada."}), 403

@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response

LOGIN_ATTEMPTS = {}
LOGIN_LOCK = threading.Lock()

def login_bloqueado(chave):
    agora = time.time()
    with LOGIN_LOCK:
        item = LOGIN_ATTEMPTS.get(chave)
        if not item:
            return False
        if agora - item["inicio"] >= 600:
            LOGIN_ATTEMPTS.pop(chave, None)
            return False
        return item["falhas"] >= 5

def registrar_falha_login(chave):
    agora = time.time()
    with LOGIN_LOCK:
        item = LOGIN_ATTEMPTS.get(chave)
        if not item or agora - item["inicio"] >= 600:
            LOGIN_ATTEMPTS[chave] = {"inicio": agora, "falhas": 1}
        else:
            item["falhas"] += 1

def limpar_falhas_login(chave):
    with LOGIN_LOCK:
        LOGIN_ATTEMPTS.pop(chave, None)

# ================= TEMPLATES HTML =================
HTML_ADM = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GESTOR DE CLIENTES</title>
    <style>
        body { background: #0a0f1d; color: white; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 10px; }
        .card { background: rgba(15, 23, 42, 0.9); padding: 15px; border-radius: 12px; border: 1px solid #00f2fe; margin-bottom: 10px; font-size: 13px; box-shadow: 0 4px 15px rgba(0, 242, 254, 0.1); }
        .user-header { cursor: pointer; display: flex; justify-content: space-between; align-items: center; padding: 5px 0; }
        .user-header:hover { color: #00f2fe; }
        .user-details { display: none; margin-top: 15px; border-top: 1px solid #1e293b; padding-top: 15px; }
        .btn-adm { padding: 10px 14px; border-radius: 6px; text-decoration: none; color: white; font-weight: bold; font-size: 11px; display: inline-block; margin: 5px 2px; border:none; cursor:pointer; text-transform: uppercase; letter-spacing: 0.5px; }
        .green { background: #10b981; } .red { background: #ef4444; } .blue { background: #3b82f6; } .orange { background: #f59e0b; }
        h2 { color: #00f2fe; text-align: center; text-transform: uppercase; letter-spacing: 1px; }
        input { background: #1e293b; color: white; border: 1px solid #334155; padding: 8px; border-radius: 6px; margin-bottom: 5px; width: 100%; box-sizing: border-box; }
        .status-badge { padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: bold; margin-left: 5px; }
        .online { background: #10b981; color: white; box-shadow: 0 0 8px rgba(16, 185, 129, 0.5); }
        .offline { background: #475569; color: #cbd5e1; }
    
        .asset-selection-box { background:#08111f; border:1px solid #1e3a4a; border-radius:12px; padding:9px; max-height:330px; overflow-y:auto; }
        .asset-selection-details { background:#08111f; border:1px solid #1e3a4a; border-radius:10px; overflow:hidden; }
        .asset-selection-details > summary { list-style:none; cursor:pointer; padding:10px 12px; color:#22d3ee; font-size:10px; font-weight:900; letter-spacing:.2px; }
        .asset-selection-details > summary::-webkit-details-marker { display:none; }
        .asset-selection-details > summary::after { content:'▼'; float:right; color:#64748b; transition:transform .15s; }
        .asset-selection-details[open] > summary::after { transform:rotate(180deg); }
        #ativos-selecao-resumo-compacto { color:#94a3b8; font-weight:700; margin-left:5px; }
        .asset-selection-content { padding:0 9px 9px; }
        .asset-category { padding:8px 0 10px; border-bottom:1px solid #172536; }
        .asset-category:last-child { border-bottom:0; }
        .asset-category-title { color:#22d3ee; font-weight:800; font-size:12px; margin-bottom:6px; }
        .asset-category-actions { display:flex; flex-wrap:wrap; gap:5px; margin-bottom:7px; }
        .asset-category-actions button, .asset-preset { border:1px solid #24566b; background:#0b2230; color:#b7e7f2; border-radius:7px; padding:5px 8px; font-size:9px; font-weight:700; cursor:pointer; }
        .asset-preset { padding:6px 9px; background:#0d2531; }
        .asset-list { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:5px; }
        .asset-list label { display:flex; align-items:center; gap:6px; min-width:0; padding:6px 7px; border:1px solid #17283a; border-radius:7px; background:#0b1421; color:#dbeafe; font-size:9px; cursor:pointer; }
        .asset-list input { accent-color:#22d3ee; flex:0 0 auto; }
        .asset-list span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .asset-list small { color:#64748b; font-size:7px; }
        @media (max-width:520px) { .asset-list { grid-template-columns:1fr; } .asset-selection-box { max-height:360px; } }
</style>
    <script>
        function toggleUser(id) {
            const el = document.getElementById(id);
            if (el.style.display === "block") {
                el.style.display = "none";
            } else {
                document.querySelectorAll('.user-details').forEach(d => d.style.display = 'none');
                el.style.display = "block";
            }
        }
    </script>
</head>
<body>
    <h2>👥 GESTÃO DE USUÁRIOS</h2>
    <p style="text-align:center; color:#94a3b8;">Total Online: {{ online_count }}</p>
    <a href="/" style="color: #00f2fe; text-decoration:none; display:block; margin-bottom: 20px; text-align: center; font-weight: bold;">⬅ Voltar ao Painel Principal</a>

    {% for email, info in lista.items() %}
    <div class="card">
        <div class="user-header" onclick="toggleUser('details-{{ loop.index }}')">
            <span>
                <b>{{ email }}</b>
                {% if email in online_list %}
                    <span class="status-badge online">ONLINE</span>
                {% else %}
                    <span class="status-badge offline">OFFLINE</span>
                {% endif %}
            </span>
            <span style="color:#00f2fe; font-size: 10px;">Exibir Dados ▾</span>
        </div>

        <div id="details-{{ loop.index }}" class="user-details">
            <div style="margin-bottom:10px;">
                <span style="color:#00f2fe;">Assertividade: <b>{{ info.winrate if info.winrate else 0 }}%</b></span><br>
                <span style="color:#94a3b8;">Wins: {{ info.wins }} | Reds: {{ info.reds }}</span><br>
                <span style="color:#f59e0b;">IPs Cadastrados (Máx 2): <b>{{ info.ips_Formatados }}</b></span><br>
                {% if info.bloqueado %}
                    <span style="color:#ef4444; font-weight:bold;">🚫 USUÁRIO BLOQUEADO</span>
                {% else %}
                    <span style="color:#10b981; font-weight:bold;">✅ USUÁRIO LIBERADO</span>
                {% endif %}
            </div>
            <form action="/adm/editar" method="POST">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <input type="hidden" name="email_original" value="{{ email }}">
                <b>E-mail:</b> <input type="text" name="novo_email" value="{{ email }}">
                <b>Nova Senha (deixe em branco para manter):</b> <input type="password" name="nova_senha" placeholder="Alterar senha...">
                <b>Expira em:</b> {{ info.criado_em }}<br><br>
                <button type="submit" class="btn-adm blue">SALVAR ALTERAÇÕES</button>
                <button type="submit" formaction="/adm/renovar/{{ email }}" formmethod="POST" class="btn-adm green">RENOVAR +30 DIAS</button>
                <button type="submit" formaction="/adm/liberar_ip/{{ email }}" formmethod="POST" class="btn-adm orange">LIBERAR DISPOSITIVOS / IPS</button>
                {% if email != admin %}
                <button type="submit" formaction="/adm/bloquear/{{ email }}" formmethod="POST" class="btn-adm {% if info.bloqueado %}green{% else %}red{% endif %}">{% if info.bloqueado %}DESBLOQUEAR USUÁRIO{% else %}BLOQUEAR USUÁRIO{% endif %}</button>
                <button type="submit" formaction="/adm/excluir/{{ email }}" formmethod="POST" class="btn-adm red" onclick="return confirm('Excluir?')">EXCLUIR</button>
                {% endif %}
            </form>
        </div>
    </div>
    {% endfor %}
</body>
</html>
"""

HTML_ESTATISTICAS = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BACKTEST REAL — VISION PRO V3</title>
<style>
body{background:#060913;color:#e2e8f0;font-family:'Segoe UI',Tahoma,sans-serif;margin:0;padding:12px}
.wrap{max-width:1180px;margin:auto}.top{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
h1{color:#00f2fe;font-size:21px;margin:0}.sub{color:#94a3b8;font-size:11px;margin-top:4px}.btn{display:inline-block;padding:10px 12px;border-radius:8px;text-decoration:none;font-weight:800;font-size:11px;border:1px solid #334155;color:#e2e8f0;background:#0f172a}
.card{background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:13px;margin-bottom:12px}.filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:9px}
label{display:block;color:#94a3b8;font-size:9px;font-weight:800;margin-bottom:5px;text-transform:uppercase}select{width:100%;box-sizing:border-box;background:#060913;color:#e2e8f0;border:1px solid #334155;border-radius:7px;padding:10px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.primary{border-color:#00f2fe;color:#00f2fe;background:rgba(0,242,254,.10);cursor:pointer}.section{color:#00f2fe;font-size:12px;font-weight:900;margin-bottom:9px;text-transform:uppercase}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:9px}.metric{background:#0b1120;border:1px solid #1e293b;border-radius:10px;padding:11px}.metric .label{margin:0}.value{font-size:21px;font-weight:900;margin-top:3px}.green{color:#10b981}.red{color:#ef4444}.cyan{color:#00f2fe}.yellow{color:#f59e0b}.muted{color:#64748b;font-size:10px;line-height:1.5}
.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:850px;font-size:10px}th,td{padding:8px 6px;border-bottom:1px solid #1e293b;text-align:left;white-space:nowrap}th{color:#94a3b8;font-size:8px;text-transform:uppercase}.best{border-left:3px solid #10b981}.source{color:#38ef7d}.unavailable{color:#f59e0b}.error{color:#ef4444;padding:10px;border:1px solid #7f1d1d;border-radius:8px;background:rgba(239,68,68,.06)}
@media(max-width:600px){body{padding:8px}h1{font-size:18px}.grid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body><div class="wrap">
<div class="top"><div><h1>🧪 BACKTEST REAL — VISION PRO V3</h1><div class="sub">Somente candles históricos fechados obtidos da fonte de mercado selecionada. Nenhum resultado de sessão é usado nesta análise.</div></div><div><a class="btn" href="/">⬅ VOLTAR AO PAINEL</a></div></div>
<div class="card">
<div class="section">🔎 CONFIGURAR ANÁLISE</div>
<form method="GET" action="/admin/estatisticas">
<div class="filters">
<div><label>Mercado</label><select name="mercado"><option value="ABERTO" {% if filtros.mercado=='ABERTO' %}selected{% endif %}>🟢 ABERTO</option><option value="OTC" {% if filtros.mercado=='OTC' %}selected{% endif %}>🌙 OTC</option><option value="AMBOS" {% if filtros.mercado=='AMBOS' %}selected{% endif %}>🌐 AMBOS</option></select></div>
<div><label>Ativo</label><select name="ativo"><option value="TODOS" {% if filtros.ativo=='TODOS' %}selected{% endif %}>Todos os ativos</option>{% for a in ativos %}<option value="{{a}}" {% if filtros.ativo==a %}selected{% endif %}>{{a}}</option>{% endfor %}</select></div>
<div><label>Timeframe</label><select name="tf"><option value="TODOS" {% if filtros.tf=='TODOS' %}selected{% endif %}>Todos os tempos</option><option value="1" {% if filtros.tf=='1' %}selected{% endif %}>M1</option><option value="5" {% if filtros.tf=='5' %}selected{% endif %}>M5</option><option value="15" {% if filtros.tf=='15' %}selected{% endif %}>M15</option></select></div>
<div><label>Estratégia</label><select name="estrategia"><option value="TODAS" {% if filtros.estrategia=='TODAS' %}selected{% endif %}>Todas as estratégias</option>{% for key,nome in estrategias.items() %}<option value="{{key}}" {% if filtros.estrategia==key %}selected{% endif %}>{{nome}}</option>{% endfor %}</select></div>
<div><label>Modo de Gale</label><select name="gale"><option value="SEM_GALE" {% if filtros.gale=='SEM_GALE' %}selected{% endif %}>SEM GALE</option><option value="GALE1" {% if filtros.gale=='GALE1' %}selected{% endif %}>COM GALE 1</option></select></div>
</div>
<div class="actions"><button class="btn primary" type="submit" name="analisar" value="1">🔍 ANALISAR DADOS REAIS AGORA</button><a class="btn" href="/admin/estatisticas">LIMPAR</a></div>
</form>
<div class="muted" style="margin-top:9px">Forex aberto: Twelve Data. Cripto aberto: Binance pública. OTC: candles reais da Quotex. O sistema nunca substitui OTC pelo preço do mercado aberto.</div>
</div>
{% if backtest_job %}
<div class="card" id="backtest-progress-card">
  <div class="section">⚙️ BACKTEST EM TEMPO REAL</div>
  <div id="backtest-progress-message" class="muted">{{ backtest_job.mensagem or 'Preparando análise com dados reais...' }}</div>
  <div style="height:10px;background:#1e293b;border-radius:8px;overflow:hidden;margin-top:10px;"><div id="backtest-progress-fill" style="height:100%;width:{{ backtest_job.percentual or 0 }}%;background:linear-gradient(90deg,#00f2fe,#10b981);transition:width .25s;"></div></div>
  <div id="backtest-progress-text" style="margin-top:8px;color:#00f2fe;font-weight:800;font-size:11px;">{{ backtest_job.percentual or 0 }}%</div>
</div>
<script>
(function(){
  const jobId = {{ backtest_job_id|tojson }};
  if(!jobId) return;
  const msg = document.getElementById('backtest-progress-message');
  const fill = document.getElementById('backtest-progress-fill');
  const pct = document.getElementById('backtest-progress-text');
  let encerrado = false;
  async function acompanhar(){
    if(encerrado) return;
    try{
      const r = await fetch('/admin/backtest/status?job=' + encodeURIComponent(jobId), {cache:'no-store'});
      const d = await r.json();
      if(!d.ok){ msg.textContent = d.error || 'Não foi possível consultar o progresso.'; return; }
      const p = Math.max(0, Math.min(100, Number(d.percentual || 0)));
      fill.style.width = p + '%';
      pct.textContent = p + '% • ' + (d.mensagem || 'Processando...');
      msg.textContent = d.mensagem || 'Processando...';
      if(d.status === 'done'){
        encerrado = true;
        window.location.href = '/admin/estatisticas?mercado=' + encodeURIComponent('{{ filtros.mercado }}') + '&ativo=' + encodeURIComponent('{{ filtros.ativo }}') + '&tf=' + encodeURIComponent('{{ filtros.tf }}') + '&estrategia=' + encodeURIComponent('{{ filtros.estrategia }}') + '&gale=' + encodeURIComponent('{{ filtros.gale }}') + '&analisar=1&job=' + encodeURIComponent(jobId);
        return;
      }
      if(d.status === 'error'){
        encerrado = true;
        msg.textContent = d.erro || d.mensagem || 'Falha na análise.';
        pct.textContent = 'ERRO';
        fill.style.width = '100%';
        fill.style.background = '#ef4444';
        return;
      }
    }catch(e){
      msg.textContent = 'Conexão temporariamente indisponível. A análise continua no servidor...';
    }
    setTimeout(acompanhar, 900);
  }
  acompanhar();
})();
</script>
{% endif %}
{% if resultado %}
<div class="grid">
<div class="metric"><div class="label">Combinações analisadas</div><div class="value cyan">{{resultado.combinacoes}}</div></div>
<div class="metric"><div class="label">Sinais avaliados</div><div class="value cyan">{{resultado.sinais}}</div></div>
<div class="metric"><div class="label">WIN</div><div class="value green">{{resultado.wins}}</div></div>
<div class="metric"><div class="label">WIN G1</div><div class="value green">{{resultado.wins_g1}}</div></div>
<div class="metric"><div class="label">RED</div><div class="value red">{{resultado.losses}}</div></div>
<div class="metric"><div class="label">Taxa histórica</div><div class="value yellow">{{'%.2f'|format(resultado.winrate)}}%</div></div>
<div class="metric"><div class="label">Melhor taxa histórica</div><div class="value green">{{'%.2f'|format(resultado.melhor_taxa)}}%</div></div>
</div>
{% if resultado.erro %}<div class="card error">{{resultado.erro}}</div>{% endif %}
<div class="card" style="margin-top:12px"><div class="section">🏆 MELHORES COMBINAÇÕES REAIS</div>
{% if resultado.linhas %}<div class="table-wrap"><table><thead><tr><th>Mercado</th><th>Ativo</th><th>Fonte</th><th>TF</th><th>Estratégia</th><th>Sinais</th><th>WIN</th><th>WIN G1</th><th>RED</th><th>Taxa histórica</th><th>Score médio</th></tr></thead><tbody>
{% for r in resultado.linhas %}<tr class="{% if loop.first %}best{% endif %}"><td>{{r.mercado}}</td><td><strong>{{r.ativo}}</strong></td><td class="source">{{r.fonte}}</td><td>M{{r.tf}}</td><td>{{r.estrategia_nome}}</td><td>{{r.total}}</td><td class="green">{{r.wins}}</td><td class="green">{{r.wins_g1}}</td><td class="red">{{r.losses}}</td><td>{{'%.2f'|format(r.winrate)}}%</td><td>{{'%.2f'|format(r.score_medio)}}</td></tr>{% endfor %}</tbody></table></div>{% else %}<div class="muted">Nenhuma combinação pôde ser analisada com dados reais no recorte selecionado.</div>{% endif %}
</div>
<div class="card"><div class="section">📌 MELHOR ESTRATÉGIA POR ATIVO + TIMEFRAME</div>{% if resultado.melhores_por_ativo %}<div class="table-wrap"><table><thead><tr><th>Ativo</th><th>TF</th><th>Estratégia</th><th>Fonte</th><th>Sinais</th><th>WIN</th><th>WIN G1</th><th>RED</th><th>Taxa histórica</th></tr></thead><tbody>{% for r in resultado.melhores_por_ativo %}<tr><td><strong>{{r.ativo}}</strong></td><td>M{{r.tf}}</td><td>{{r.estrategia_nome}}</td><td class="source">{{r.fonte}}</td><td>{{r.total}}</td><td class="green">{{r.wins}}</td><td class="green">{{r.wins_g1}}</td><td class="red">{{r.losses}}</td><td>{{'%.2f'|format(r.winrate)}}%</td></tr>{% endfor %}</tbody></table></div>{% else %}<div class="muted">Sem dados reais suficientes.</div>{% endif %}</div>
{% endif %}
</div>
<script>
// Mantém uma pequena atividade HTTP enquanto a tela de backtest está aberta.
// O backtest e o bot continuam em threads separadas no servidor.
setInterval(() => { fetch('/status', {cache:'no-store'}).catch(() => {}); }, 15000);
</script>
</body></html>
"""

HTML_TERMOS = """
<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>TERMOS DE USO</title><style>
    body { background: #0a0f1d; color: white; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 20px; line-height: 1.6; }
    .card { background: #0f172a; padding: 25px; border-radius: 15px; max-width: 600px; margin: auto; border: 1px solid #00f2fe; box-shadow: 0 0 20px rgba(0,242,254,0.15); }
    h2 { color: #00f2fe; border-bottom: 1px solid #1e293b; padding-bottom: 10px; text-transform: uppercase; }
    p { font-size: 14px; color: #94a3b8; }
    .btn { display: block; text-align: center; background: linear-gradient(135deg, #00c6ff, #0072ff); color: white; padding: 14px; border-radius: 8px; text-decoration: none; font-weight: bold; margin-top: 20px; box-shadow: 0 4px 15px rgba(0,198,255,0.4); }
</style></head>
<body>
    <div class="card">
        <h2>⚖️ TERMOS DE USO E RESPONSABILIDADE</h2>
        <p>1. <b>NATUREZA DO SERVIÇO:</b> O Vision Pro V3 é uma ferramenta de análise estatística baseada em algoritmos de inteligência artificial e indicadores técnicos. Não garantimos lucros.</p>
        <p>2. <b>RISCO DE MERCADO:</b> O mercado financeiro (Forex e Cripto) envolve riscos elevados. Você pode perder parte ou todo o seu capital.</p>
        <p>3. <b>RESPONSABILIDADE:</b> O usuário é o único responsável por suas operações. O software apenas emite alertas baseados em padrões históricos.</p>
        <p>4. <b>LIMITAÇÃO:</b> Não somos uma corretora ou casa de análise financeira regulamentada. Use este bot para fins de auxílio educacional e operacional próprio.</p>
        <a href="/login" class="btn">LI E CONCORDO</a>
    </div>
</body>
</html>
"""

HTML_LOGIN = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LOGIN - VISION PRO ULTRA</title>
    <style>
        body { background: #060913; color: white; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .login-card { background: rgba(15, 23, 42, 0.95); padding: 35px 30px; border-radius: 20px; width: 90%; max-width: 360px; text-align: center; border: 1px solid rgba(0, 242, 254, 0.3); box-shadow: 0 10px 30px rgba(0, 242, 254, 0.15); backdrop-filter: blur(10px); }
        h2 { color: #00f2fe; margin-bottom: 25px; letter-spacing: 1.5px; text-transform: uppercase; font-size: 22px; text-shadow: 0 0 10px rgba(0,242,254,0.5); }
        input { width: 100%; box-sizing: border-box; padding: 14px; margin: 10px 0; border-radius: 10px; border: 1px solid #1e293b; background: #0f172a; color: white; font-size: 15px; outline: none; transition: 0.3s; }
        input:focus { border-color: #00f2fe; box-shadow: 0 0 10px rgba(0,242,254,0.3); }
        button { width: 100%; padding: 14px; background: linear-gradient(135deg, #00c6ff, #0072ff); border: none; color: white; border-radius: 10px; cursor: pointer; font-weight: bold; font-size: 15px; margin-top: 15px; letter-spacing: 1px; box-shadow: 0 4px 15px rgba(0,198,255,0.4); transition: 0.3s; }
        button:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0,198,255,0.6); }
        .links { margin-top: 25px; font-size: 13px; }
        a { color: #00f2fe; text-decoration: none; margin: 0 8px; font-weight: 500; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="login-card">
        <h2>VISION PRO V3</h2>
        {% if erro %}<div style="color:#ef4444; margin-bottom:15px; font-size:13px; background:rgba(239,68,68,0.1); padding:10px; border-radius:8px; border:1px solid rgba(239,68,68,0.3);">{{erro}}</div>{% endif %}
        <form method="POST" action="/login">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <input type="email" name="email" placeholder="Seu E-mail" required>
            <input type="password" name="password" placeholder="Sua Senha" required>
            <button type="submit">ACESSAR O TERMINAL</button>
        </form>
        <div class="links">
            <a href="/register">Criar Conta</a> | <a href="/termos" style="color:#94a3b8">Termos de Uso</a>
        </div>
    </div>
</body>
</html>
"""

HTML_REGISTER = """
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CADASTRO - VISION PRO ULTRA</title>
    <style>
        body { background: #060913; color: white; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
        .login-card { background: rgba(15, 23, 42, 0.95); padding: 35px 30px; border-radius: 20px; width: 90%; max-width: 360px; text-align: center; border: 1px solid rgba(16, 185, 129, 0.3); box-shadow: 0 10px 30px rgba(16, 185, 129, 0.15); backdrop-filter: blur(10px); }
        h2 { color: #10b981; margin-bottom: 25px; letter-spacing: 1.5px; text-transform: uppercase; font-size: 22px; text-shadow: 0 0 10px rgba(16,185,129,0.5); }
        input { width: 100%; box-sizing: border-box; padding: 14px; margin: 10px 0; border-radius: 10px; border: 1px solid #1e293b; background: #0f172a; color: white; font-size: 15px; outline: none; transition: 0.3s; }
        input:focus { border-color: #10b981; box-shadow: 0 0 10px rgba(16,185,129,0.3); }
        button { width: 100%; padding: 14px; background: linear-gradient(135deg, #10b981, #059669); border: none; color: white; border-radius: 10px; cursor: pointer; font-weight: bold; font-size: 15px; margin-top: 15px; letter-spacing: 1px; box-shadow: 0 4px 15px rgba(16,185,129,0.4); transition: 0.3s; }
        button:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(16,185,129,0.6); }
        a { color: #00f2fe; text-decoration: none; font-size: 13px; display: block; margin-top: 20px; font-weight: 500; }
    </style>
</head>
<body>
    <div class="login-card">
        <h2>CRIAR CONTA NOVA</h2>
        {% if erro %}<div style="color:#ef4444; margin-bottom:15px; font-size:13px; background:rgba(239,68,68,0.1); padding:10px; border-radius:8px; border:1px solid rgba(239,68,68,0.3);">{{erro}}</div>{% endif %}
        <form method="POST" action="/register">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
            <input type="email" name="email" placeholder="Novo E-mail" required>
            <input type="password" name="password" placeholder="Nova Senha" required>
            <button type="submit">CONCLUIR CADASTRO</button>
        </form>
        <a href="/login">Já possui uma conta? Faça Login</a>
    </div>
</body>
</html>
"""

HTML_INDEX = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VISION PRO V3 - HIGH FREQUENCY BOT ANALYTICS</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; }
        body { background-color: #060913; color: #f1f5f9; display: flex; justify-content: center; min-height: 100vh; padding: 15px 10px; }
        
        .container {
            width: 100%;
            max-width: 520px;
            background: rgba(15, 23, 42, 0.8);
            border: 1px solid rgba(0, 242, 254, 0.2);
            border-radius: 24px;
            padding: 20px;
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.8), 0 0 20px rgba(0, 242, 254, 0.05);
            backdrop-filter: blur(12px);
        }

        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid rgba(255, 255, 255, 0.08); }
        .brand { font-size: 17px; font-weight: 900; letter-spacing: 1px; color: #00f2fe; display: flex; align-items: center; gap: 8px; text-shadow: 0 0 10px rgba(0,242,254,0.4); }
        .brand span { background: rgba(0, 242, 254, 0.15); color: #38ef7d; font-size: 10px; padding: 3px 8px; border-radius: 12px; border: 1px solid rgba(56, 239, 125, 0.4); font-weight: 700; }
        .btn-logout { font-size: 12px; color: #ef4444; text-decoration: none; font-weight: 700; padding: 6px 14px; border-radius: 10px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.25); transition: 0.2s; }
        .btn-logout:hover { background: rgba(239, 68, 68, 0.2); }

        .placar-card { background: #0b1120; border: 1px solid #1e293b; border-radius: 16px; padding: 16px; margin-bottom: 16px; box-shadow: inset 0 2px 4px rgba(0,0,0,0.5); }
        .placar-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; text-align: center; }
        .placar-item .title { font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: 700; margin-bottom: 4px; letter-spacing: 0.5px; }
        .placar-item .val { font-size: 20px; font-weight: 800; font-family: 'JetBrains Mono', monospace; }
        .win-color { color: #10b981; text-shadow: 0 0 10px rgba(16,185,129,0.3); }
        .loss-color { color: #ef4444; text-shadow: 0 0 10px rgba(239,68,68,0.3); }
        .wr-color { color: #3b82f6; text-shadow: 0 0 10px rgba(59,130,246,0.3); }
        .winrate-bar { height: 6px; background: #1e293b; border-radius: 10px; overflow: hidden; margin-top: 14px; }
        .winrate-fill { height: 100%; background: linear-gradient(90deg, #059669, #10b981); width: 0%; transition: width 0.5s ease-in-out; }

        #broker-view-container { display: none; width: 100%; height: 350px; border-radius: 16px; overflow: hidden; flex-direction: column; margin-bottom: 16px; background: #0b1120; border: 1px solid #1e293b; padding: 8px; }
        .broker-iframe-inline { width: 100%; height: 100%; border: none; background: #0b1120; border-radius: 10px; }
        .btn-close-broker { background: #1e293b; border: 1px solid #334155; color: #00f2fe; padding: 6px 12px; font-size: 11px; font-weight: 700; border-radius: 6px; cursor: pointer; margin-bottom: 8px; width: 100%; text-align: center; }

        .status-box { background: linear-gradient(145deg, #0f172a, #0b1120); border: 1px solid rgba(0, 242, 254, 0.3); padding: 18px; border-radius: 16px; margin-bottom: 16px; min-height: 100px; text-align: center; display: flex; flex-direction: column; align-items: center; justify-content: center; font-size: 14px; font-weight: 600; box-shadow: inset 0 2px 4px rgba(0,0,0,0.6), 0 0 15px rgba(0, 242, 254, 0.08); }
        
        .system-console { font-family: 'JetBrains Mono', monospace; color: #38ef7d; font-size: 13px; text-shadow: 0 0 5px rgba(56, 239, 125, 0.5); width: 100%; }

        .result-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 16px; }
        .btn-res { border: none; padding: 12px; border-radius: 10px; font-weight: 800; font-size: 12px; cursor: pointer; color: white; transition: transform 0.1s, box-shadow 0.2s; text-transform: uppercase; }
        .btn-res:active { transform: scale(0.95); }
        .btn-res-win { background: linear-gradient(135deg, #10b981, #059669); box-shadow: 0 4px 12px rgba(16,185,129,0.3); }
        .btn-res-g1 { background: linear-gradient(135deg, #f59e0b, #d97706); color: #000; box-shadow: 0 4px 12px rgba(245,158,11,0.3); }
        .btn-res-red { background: linear-gradient(135deg, #ef4444, #dc2626); box-shadow: 0 4px 12px rgba(239,68,68,0.3); }
        .btn-res-skip { background: #334155; box-shadow: 0 4px 12px rgba(51,65,85,0.3); }

        .control-panel { background: #0b1120; border: 1px solid #1e293b; border-radius: 16px; padding: 15px; margin-bottom: 16px; }
        .section-label { font-size: 11px; font-weight: 800; color: #64748b; text-transform: uppercase; margin-bottom: 10px; letter-spacing: 1px; display: block; border-bottom: 1px solid #1e293b; padding-bottom: 5px;}
        
        .action-flex { display: flex; gap: 8px; margin-bottom: 15px; }
        .btn-action { flex: 1; padding: 12px 5px; border: none; border-radius: 10px; font-weight: 800; font-size: 12px; color: white; cursor: pointer; transition: 0.2s; text-transform: uppercase; }
        .btn-action:active { transform: scale(0.95); }
        .btn-start { background: linear-gradient(135deg, #10b981, #059669); box-shadow: 0 4px 12px rgba(16,185,129,0.2); }
        .btn-pause { background: linear-gradient(135deg, #f59e0b, #d97706); box-shadow: 0 4px 12px rgba(245,158,11,0.2); }
        .btn-stop { background: linear-gradient(135deg, #ef4444, #dc2626); box-shadow: 0 4px 12px rgba(239,68,68,0.2); }

        .settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 15px; }
        .settings-grid.full { grid-template-columns: 1fr; margin-bottom: 15px; }
        .setting-group label { font-size: 10px; font-weight: 700; color: #94a3b8; margin-bottom: 4px; display: block; }
        
        .select-wrapper { position: relative; width: 100%; }
        .select-wrapper::after { content: "▼"; position: absolute; right: 12px; top: 12px; color: #00f2fe; font-size: 10px; pointer-events: none; }
        .modern-select { background: #0f172a; color: #f1f5f9; border: 1px solid #1e293b; padding: 10px 12px; border-radius: 8px; font-weight: 600; font-size: 12px; width: 100%; outline: none; appearance: none; cursor: pointer; transition: 0.2s; }
        .modern-select:hover, .modern-select:focus { border-color: #00f2fe; box-shadow: 0 0 8px rgba(0,242,254,0.2); }

        .btn-toggle-hist { width: 100%; padding: 10px; background: rgba(0, 242, 254, 0.08); border: 1px dashed #00f2fe; color: #00f2fe; border-radius: 8px; font-weight: bold; font-size: 11px; cursor: pointer; margin-top: 10px; transition: 0.3s; }
        .btn-toggle-hist:hover { background: rgba(0, 242, 254, 0.2); }

        .btn-test-tg { width: 100%; padding: 10px; background: rgba(59, 130, 246, 0.15); border: 1px solid #3b82f6; color: #3b82f6; font-weight: bold; font-size: 11px; border-radius: 8px; cursor: pointer; margin-bottom: 8px; transition: 0.3s; text-transform: uppercase; }
        .btn-test-tg:hover { background: rgba(59, 130, 246, 0.3); }

        .historico-box { display: none; background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; padding: 12px; margin-top: 15px; }
        .historico-scroll { max-height: 140px; overflow-y: auto; }
        .historico-item { font-size: 11px; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.05); display: flex; justify-content: space-between; align-items: center; font-family: 'JetBrains Mono', monospace; }
        .historico-item:last-child { border-bottom: none; }

        .tech-scanner { width: 28px; height: 28px; margin: 10px auto 0; border: 3px solid rgba(0, 242, 254, 0.2); border-top-color: #00f2fe; border-radius: 50%; animation: spin 0.8s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }

        .btn-notify { width: 100%; padding: 10px; background: rgba(16, 185, 129, 0.15); border: 1px solid #10b981; color: #10b981; font-weight: bold; font-size: 11px; border-radius: 8px; cursor: pointer; margin-bottom: 12px; transition: 0.3s; text-transform: uppercase; }
        .btn-notify:hover { background: rgba(16, 185, 129, 0.3); }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="brand">VISION PRO <span>V3 ULTRA</span></div>
            <a href="/logout" class="btn-logout">SAIR</a>
        </div>

        <button class="btn-notify" type="button" onclick="toggleNotificacoes()">🔔 NOTIFICAÇÕES E TELEGRAM ▾</button>
        <div id="notificacoes-box" style="display:none; margin-bottom:12px;">
            <button class="btn-notify" id="btn-enable-notify" onclick="solicitarPermissaoNotificacao()">🔔 ATIVAR NOTIFICAÇÕES NO CELULAR</button>
            {% if user == admin %}
            <button class="btn-test-tg" id="btn-telegram-toggle" onclick="sendCommand('toggle_telegram')">📡 TELEGRAM: CARREGANDO...</button>
            <button class="btn-test-tg" onclick="sendCommand('test_telegram')">🧪 TESTAR CONEXÃO TELEGRAM</button>
            {% endif %}
        </div>

        <div class="placar-card">
            <div class="placar-grid">
                <div class="placar-item">
                    <div class="title">WINS</div>
                    <div class="val win-color" id="win-count">0</div>
                </div>
                <div class="placar-item">
                    <div class="title">ASSERTIVIDADE</div>
                    <div class="val wr-text" id="wr-text">0%</div>
                </div>
                <div class="placar-item">
                    <div class="title">LOSS</div>
                    <div class="val loss-color" id="loss-count">0</div>
                </div>
            </div>
            <div class="winrate-bar"><div id="wr-fill" class="winrate-fill"></div></div>
        </div>

        <div id="ticker-live-status" style="background: rgba(0, 242, 254, 0.05); border: 1px solid rgba(0, 242, 254, 0.2); border-radius: 12px; padding: 10px; margin-bottom: 12px; text-align: center; font-size: 12px;">
            MERCADO: <b id="mkt-badge" style="color: #00f2fe;">{{ modo }}</b><br>
            ATIVO EM ANÁLISE: <b id="current-asset" style="color: #38ef7d;">AGUARDANDO...</b><br>
            <div id="candle-timer" style="margin-top:7px; color:#94a3b8; font-family:'JetBrains Mono',monospace; font-size:11px;">
                CANDLE M{{ tf }} • 00:00 DECORRIDOS • 00:00 RESTANTES
            </div>
        </div>

        <div class="status-box" id="panel-text">Aguardando Comando...</div>

        <div id="result-area" class="result-grid" style="display:none;">
            <button class="btn-res btn-res-win" onclick="sendResult('win')">WIN</button>
            <button class="btn-res btn-res-g1" onclick="sendResult('g1')">G1</button>
            <button class="btn-res btn-res-red" onclick="sendResult('red')">RED</button>
            <button class="btn-res btn-res-skip" onclick="sendResult('pular')">PULAR</button>
        </div>

        <div class="control-panel">
            <span class="section-label">Controles do Robô</span>
            
            <div class="action-flex">
                <button class="btn-action btn-start" onclick="sendCommand('start_bot')">▶ START</button>
                <button class="btn-action btn-pause" onclick="sendCommand('pause_bot')">⏸ PAUSE</button>
                <button class="btn-action btn-stop" onclick="sendCommand('stop_bot')">⏹ STOP</button>
            </div>

            <span class="section-label">Configurações de Análise</span>
            
            <div class="settings-grid">
                <div class="setting-group">
                    <label>TIPO DE MERCADO</label>
                    <div class="select-wrapper">
                        <select class="modern-select" onchange="sendCommand('mkt_' + this.value)">
                            <option value="TODOS" {% if modo == 'TODOS' %}selected{% endif %}>🌐 Todos os Mercados (Aberto + OTC)</option>
                            <option value="ABERTO_TODOS" {% if modo == 'ABERTO_TODOS' %}selected{% endif %}>🟢 Todo Mercado Aberto (Forex + Cripto)</option>
                            <option value="OTC_TODOS" {% if modo == 'OTC_TODOS' %}selected{% endif %}>🌙 Todo Mercado OTC (Forex + Cripto)</option>
                            <option value="FOREX_ABERTO" {% if modo == 'FOREX_ABERTO' %}selected{% endif %}>📈 Forex Aberto (Seg a Sex)</option>
                            <option value="CRIPTO_ABERTO" {% if modo == 'CRIPTO_ABERTO' %}selected{% endif %}>🪙 Criptomoedas Aberto (24/7)</option>
                            <option value="FOREX_OTC" {% if modo == 'FOREX_OTC' %}selected{% endif %}>📊 Forex OTC (Noite/FDS)</option>
                            <option value="CRIPTO_OTC" {% if modo == 'CRIPTO_OTC' %}selected{% endif %}>⚡ Cripto OTC (Noite/FDS)</option>
                        </select>
                    </div>
                </div>
                <div class="setting-group">
                    <label>TIMEFRAME</label>
                    <div class="select-wrapper">
                        <select class="modern-select" onchange="sendCommand('tf_' + this.value)">
                            <option value="1" {% if tf == 1 %}selected{% endif %}>M1 (1 Minuto)</option>
                            <option value="5" {% if tf == 5 %}selected{% endif %}>M5 (5 Minutos)</option>
                            <option value="15" {% if tf == 15 %}selected{% endif %}>M15 (15 Minutos)</option>
                        </select>
                    </div>
                </div>
            </div>

            <div class="settings-grid full">
                <div class="setting-group">
                    <label>ATIVOS PARA OPERAR</label>
                    <details class="asset-selection-details">
                        <summary>⚙️ CONFIGURAR ATIVOS <span id="ativos-selecao-resumo-compacto">{{ resumo_ativos }}</span></summary>
                        <div class="asset-selection-content">
                            <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;">
                                <button type="button" class="asset-preset" onclick="selecionarPresetAtivos('TODOS')">🌐 TODOS</button>
                                <button type="button" class="asset-preset" onclick="selecionarPresetAtivos('ABERTOS')">🟢 ABERTOS</button>
                                <button type="button" class="asset-preset" onclick="selecionarPresetAtivos('OTC')">🌙 OTC</button>
                                <button type="button" class="asset-preset" onclick="selecionarPresetAtivos('FOREX')">📈 FOREX</button>
                                <button type="button" class="asset-preset" onclick="selecionarPresetAtivos('CRIPTO')">🪙 CRIPTO</button>
                            </div>
                            <div class="asset-selection-box">
                                <div class="asset-category">
                                    <div class="asset-category-title">📈 FOREX</div>
                                    <div class="asset-category-actions">
                                        <button type="button" onclick="selecionarPresetAtivos('FOREX_ABERTO')">🟢 Abertos</button>
                                        <button type="button" onclick="selecionarPresetAtivos('FOREX_OTC')">🌙 OTC</button>
                                    </div>
                                    <div class="asset-list">
                                        {% for a in ATIVOS_BASE['FOREX_ABERTO'] %}<label><input type="checkbox" class="ativo-check" value="{{a}}" {% if a in ativos_selecionados %}checked{% endif %} onchange="alterarAtivosIndividuais()"><span>{{a}} <small>ABERTO</small></span></label>{% endfor %}
                                        {% for a in ATIVOS_BASE['FOREX_OTC'] %}<label><input type="checkbox" class="ativo-check" value="{{a}}" {% if a in ativos_selecionados %}checked{% endif %} onchange="alterarAtivosIndividuais()"><span>{{a}} <small>OTC</small></span></label>{% endfor %}
                                    </div>
                                </div>
                                <div class="asset-category">
                                    <div class="asset-category-title">🪙 CRIPTOMOEDAS</div>
                                    <div class="asset-category-actions">
                                        <button type="button" onclick="selecionarPresetAtivos('CRIPTO_ABERTO')">🟢 Abertas</button>
                                        <button type="button" onclick="selecionarPresetAtivos('CRIPTO_OTC')">🌙 OTC</button>
                                    </div>
                                    <div class="asset-list">
                                        {% for a in ATIVOS_BASE['CRIPTO_ABERTO'] %}<label><input type="checkbox" class="ativo-check" value="{{a}}" {% if a in ativos_selecionados %}checked{% endif %} onchange="alterarAtivosIndividuais()"><span>{{a}} <small>ABERTO</small></span></label>{% endfor %}
                                        {% for a in ATIVOS_BASE['CRIPTO_OTC'] %}<label><input type="checkbox" class="ativo-check" value="{{a}}" {% if a in ativos_selecionados %}checked{% endif %} onchange="alterarAtivosIndividuais()"><span>{{a}} <small>OTC</small></span></label>{% endfor %}
                                    </div>
                                </div>
                            </div>
                            <div id="ativos-selecao-resumo" style="font-size:10px;color:#94a3b8;margin-top:7px;line-height:1.4;">Seleção: <b style="color:#22d3ee;">{{ resumo_ativos }}</b></div>
                        </div>
                    </details>
                </div>
            </div>
            
            <div class="settings-grid full">
                <div class="setting-group">
                    <label>ESTRATÉGIA OPERACIONAL</label>
                    <div class="select-wrapper">
                        <select class="modern-select" onchange="sendCommand('set_est_' + this.value)">
                            <option value="TODAS" {% if estrat == 'TODAS' %}selected{% endif %}>💎 TODAS (Analisar Todas as Estratégias)</option>
                            <option value="PRICE_ACTION" {% if estrat == 'PRICE_ACTION' %}selected{% endif %}>🎯 Price Action Profissional</option>
                            <option value="LOGICA_DO_PRECO" {% if estrat == 'LOGICA_DO_PRECO' %}selected{% endif %}>Lógica do Preço</option>
                            <option value="RSI_MACD_MA" {% if estrat == 'RSI_MACD_MA' %}selected{% endif %}>RSI + Cruzamento MACD + MA</option>
                            <option value="MHI1" {% if estrat == 'MHI1' %}selected{% endif %}>MHI 1 (+ Filtro Tendência)</option>
                            <option value="REVERSAO" {% if estrat == 'REVERSAO' %}selected{% endif %}>Reversão de Bandas</option>
                        </select>
                    </div>
                </div>
            </div>

            {% if user == admin %}
            <a class="btn-toggle-hist" href="/admin_panel" style="display:block;text-align:center;text-decoration:none;margin-bottom:8px;">👑 PAINEL ADM — GESTÃO DE CLIENTES</a>
            <button class="btn-toggle-hist" type="button" onclick="toggleEstatisticas()">📊 ESTATÍSTICAS / BACKTEST REAL ▾</button>
            <div id="estatisticas-opcoes" style="display:none; margin-top:10px; background:#0f172a; border:1px solid #1e293b; border-radius:12px; padding:12px;">
                <div class="section-label">Configurar análise com dados reais</div>
                <div class="settings-grid">
                    <div class="setting-group"><label>MERCADO</label><div class="select-wrapper"><select id="bt-mercado" class="modern-select"><option value="ABERTO">🟢 ABERTO</option><option value="OTC">🌙 OTC</option><option value="AMBOS">🌐 AMBOS</option></select></div></div>
                    <div class="setting-group"><label>ATIVO</label><div class="select-wrapper"><select id="bt-ativo" class="modern-select"><option value="TODOS">TODOS OS ATIVOS</option>{% for a in (ATIVOS_BASE.get('FOREX_ABERTO', []) + ATIVOS_BASE.get('CRIPTO_ABERTO', []) + ATIVOS_BASE.get('FOREX_OTC', []) + ATIVOS_BASE.get('CRIPTO_OTC', [])) %}<option value="{{a}}">{{a}}</option>{% endfor %}</select></div></div>
                    <div class="setting-group"><label>TIMEFRAME</label><div class="select-wrapper"><select id="bt-tf" class="modern-select"><option value="TODOS">TODOS</option><option value="1">M1</option><option value="5">M5</option><option value="15">M15</option></select></div></div>
                    <div class="setting-group"><label>ESTRATÉGIA</label><div class="select-wrapper"><select id="bt-estrategia" class="modern-select"><option value="TODAS">TODAS</option>{% for key,nome in NOME_ESTRATEGIAS_DISPLAY.items() if key in LISTA_ESTRATEGIAS %}<option value="{{key}}">{{nome}}</option>{% endfor %}</select></div></div>
                    <div class="setting-group"><label>MODO DE GALE</label><div class="select-wrapper"><select id="bt-gale" class="modern-select"><option value="SEM_GALE">SEM GALE</option><option value="GALE1">COM GALE 1</option></select></div></div>
                </div>
                <button class="btn-toggle-hist" type="button" onclick="abrirBacktestConfigurado()">🔍 ANALISAR DADOS REAIS</button>
            </div>
            {% endif %}

            <button class="btn-toggle-hist" onclick="toggleHistorico()">👁️ EXIBIR HISTÓRICO PASSADO</button>

            <div class="historico-box" id="box-historico">
                <span class="section-label">Histórico de Sinais Salvo</span>
                <div class="historico-scroll" id="lista-sinais"></div>
            </div>
        </div>

    </div>

    <script>
        let lastNotifId = null;
        const NATIVE_NOTIFICATION_COOLDOWN_MS = 60000;

        // Registra o Service Worker, mas não dispara nenhuma notificação automaticamente.
        if ('serviceWorker' in navigator && 'Notification' in window) {
            navigator.serviceWorker.register('/sw.js', { updateViaCache: 'none' })
                .then(() => console.log('Service Worker de notificações registrado.'))
                .catch(err => console.warn('Falha ao registrar Service Worker:', err));
        }

        function solicitarPermissaoNotificacao() {
            if (!('Notification' in window)) {
                alert('Este navegador não suporta notificações de sistema.');
                return;
            }

            Notification.requestPermission().then(permission => {
                if (permission === 'granted') {
                    const btn = document.getElementById('btn-enable-notify');
                    btn.innerText = "✅ NOTIFICAÇÕES NATIVAS ATIVADAS!";
                    btn.style.borderColor = "#10b981";
                    btn.style.color = "#10b981";

                    // Não envia uma notificação de teste imediatamente após a permissão.
                    // Isso evita uma notificação desnecessária no momento da ativação.
                } else {
                    alert('Permissão de Notificação Recusada.');
                }
            });
        }

        async function dispararNotificacaoNativa(titulo, corpo, notifId) {
            if (!('Notification' in window) || Notification.permission !== 'granted') return;

            const id = String(notifId || '');
            const agora = Date.now();

            // Evita repetir a mesma notificação após recarregar/consultar o painel.
            const ultimoId = localStorage.getItem('vision_last_notif_id') || '';
            const ultimaHora = Number(localStorage.getItem('vision_last_notif_at') || '0');

            if (id && id === ultimoId) return;
            if (ultimaHora && (agora - ultimaHora) < NATIVE_NOTIFICATION_COOLDOWN_MS) return;

            try {
                if ('serviceWorker' in navigator) {
                    const reg = await navigator.serviceWorker.ready;

                    await reg.showNotification(titulo, {
                        body: corpo,
                        // Sem vibração repetitiva e sem renotify: comportamento menos intrusivo.
                        tag: 'vision-signal',
                        renotify: false,
                        requireInteraction: false
                    });
                } else {
                    new Notification(titulo, { body: corpo });
                }

                if (id) localStorage.setItem('vision_last_notif_id', id);
                localStorage.setItem('vision_last_notif_at', String(agora));
            } catch (err) {
                console.warn('Não foi possível exibir a notificação:', err);
            }
        }

        function toggleNotificacoes() {
            const box = document.getElementById('notificacoes-box');
            if (!box) return;
            box.style.display = box.style.display === 'none' ? 'block' : 'none';
        }

        function toggleEstatisticas() {
            const box = document.getElementById('estatisticas-opcoes');
            if (!box) return;
            box.style.display = box.style.display === 'none' ? 'block' : 'none';
        }

        function abrirBacktestConfigurado() {
            const mercado = document.getElementById('bt-mercado')?.value || 'ABERTO';
            const ativo = document.getElementById('bt-ativo')?.value || 'TODOS';
            const tf = document.getElementById('bt-tf')?.value || 'TODOS';
            const estrategia = document.getElementById('bt-estrategia')?.value || 'TODAS';
            const gale = document.getElementById('bt-gale')?.value || 'SEM_GALE';
            const params = new URLSearchParams({mercado, ativo, tf, estrategia, gale, analisar:'1'});
            window.location.href = '/admin/estatisticas?' + params.toString();
        }

        function toggleHistorico() {
            const box = document.getElementById('box-historico');
            if (box.style.display === 'block') {
                box.style.display = 'none';
            } else {
                box.style.display = 'block';
            }
        }

        function atualizarResumoAtivos(valores) {
            const el = document.getElementById('ativos-selecao-resumo');
            if (!el) return;
            if (!valores || valores.length === 0) {
                el.innerHTML = 'Seleção: <b style="color:#f59e0b;">TODOS</b>';
                const compact = document.getElementById('ativos-selecao-resumo-compacto');
                if (compact) compact.textContent = 'TODOS';
                return;
            }
            const resumo = valores.length === 1 ? valores[0] : valores.length + ' ATIVOS SELECIONADOS';
            el.innerHTML = 'Seleção: <b style="color:#22d3ee;">' + resumo + '</b>';
            const compact = document.getElementById('ativos-selecao-resumo-compacto');
            if (compact) compact.textContent = resumo;
        }

        function enviarSelecaoAtivos(valores) {
            const limpos = [...new Set((valores || []).filter(Boolean))];
            const payload = limpos.length ? limpos.join(',') : 'TODOS';
            sendCommand('ativos_' + encodeURIComponent(payload));
            atualizarResumoAtivos(limpos.length ? limpos : ['TODOS']);
        }

        function selecionarPresetAtivos(preset) {
            document.querySelectorAll('.ativo-check').forEach(cb => cb.checked = false);
            const grupos = {
                TODOS: [],
                ABERTOS: ['FOREX_ABERTO','CRIPTO_ABERTO'],
                OTC: ['FOREX_OTC','CRIPTO_OTC'],
                FOREX: ['FOREX_ABERTO','FOREX_OTC'],
                CRIPTO: ['CRIPTO_ABERTO','CRIPTO_OTC'],
                FOREX_ABERTO: ['FOREX_ABERTO'],
                FOREX_OTC: ['FOREX_OTC'],
                CRIPTO_ABERTO: ['CRIPTO_ABERTO'],
                CRIPTO_OTC: ['CRIPTO_OTC']
            };
            const listas = {
                FOREX_ABERTO: [{% for a in ATIVOS_BASE['FOREX_ABERTO'] %}'{{a}}',{% endfor %}],
                FOREX_OTC: [{% for a in ATIVOS_BASE['FOREX_OTC'] %}'{{a}}',{% endfor %}],
                CRIPTO_ABERTO: [{% for a in ATIVOS_BASE['CRIPTO_ABERTO'] %}'{{a}}',{% endfor %}],
                CRIPTO_OTC: [{% for a in ATIVOS_BASE['CRIPTO_OTC'] %}'{{a}}',{% endfor %}]
            };
            if (preset === 'TODOS') {
                enviarSelecaoAtivos(['TODOS']);
                return;
            }
            let valores = [];
            (grupos[preset] || []).forEach(g => valores.push(...(listas[g] || [])));
            valores = [...new Set(valores)];
            document.querySelectorAll('.ativo-check').forEach(cb => cb.checked = valores.includes(cb.value));
            enviarSelecaoAtivos(valores);
        }

        function alterarAtivosIndividuais() {
            const valores = Array.from(document.querySelectorAll('.ativo-check:checked')).map(cb => cb.value);
            enviarSelecaoAtivos(valores);
        }

        function sendCommand(cmd) {
            fetch('/command/' + cmd, { method: 'POST', headers: {'X-CSRF-Token': '{{ csrf_token() }}'} }).then(r => r.json()).then(data => {
                if(data.redirect) window.location.href = data.redirect;
            });
        }

        function sendResult(resultado) {
            fetch('/resultado/' + resultado, { method: 'POST', headers: {'X-CSRF-Token': '{{ csrf_token() }}'} })
                .then(r => r.json())
                .catch(() => {});
        }

        let timeframeCronometro = 5;

        function formatarTempoCandle(segundos) {
            const total = Math.max(0, Math.floor(segundos));
            const minutos = String(Math.floor(total / 60)).padStart(2, '0');
            const segundosRestantes = String(total % 60).padStart(2, '0');
            return `${minutos}:${segundosRestantes}`;
        }

        function atualizarCronometroCandle(tf) {
            const elemento = document.getElementById('candle-timer');
            if (!elemento) return;

            const tfAtual = Number(tf) || 5;
            timeframeCronometro = tfAtual;

            // O relógio do candle é calculado localmente pelo relógio real do navegador.
            // Assim ele não depende do ciclo de atualização do Flask e não sofre pausas
            // quando uma consulta /status demora para responder.
            const duracao = tfAtual * 60;
            const agoraMs = Date.now();
            const segundoAtual = Math.floor(agoraMs / 1000);
            const decorrido = segundoAtual % duracao;
            const restante = duracao - decorrido;

            elemento.innerText = `CANDLE M${tfAtual} • ${formatarTempoCandle(decorrido)} DECORRIDOS • ${formatarTempoCandle(restante)} RESTANTES`;
        }

        // Atualização independente do servidor: o cronômetro continua correndo
        // de segundo em segundo mesmo enquanto o painel consulta /status.
        setInterval(() => atualizarCronometroCandle(timeframeCronometro), 1000);

        async function atualizarPainel() {
            try {
                const r = await fetch('/status', { cache: 'no-store' });
                const data = await r.json();
                const panel = document.getElementById('panel-text');
                if(panel && data.html) panel.innerHTML = data.html;
                if(document.getElementById('win-count')) document.getElementById('win-count').innerText = data.wins;
                if(document.getElementById('loss-count')) document.getElementById('loss-count').innerText = data.reds;
                if(document.getElementById('wr-text')) document.getElementById('wr-text').innerText = data.winrate + "%";
                if(document.getElementById('wr-fill')) document.getElementById('wr-fill').style.width = data.winrate + "%";
                if(document.getElementById('result-area')) document.getElementById('result-area').style.display = data.aguardando ? 'grid' : 'none';
                
                if(document.getElementById('mkt-badge')) document.getElementById('mkt-badge').innerText = data.mercado || "TODOS";
                if(document.getElementById('current-asset')) {
                    if(data.rodando) {
                        document.getElementById('current-asset').innerText = data.ativo_atual || "VARRENDO...";
                    } else {
                        document.getElementById('current-asset').innerText = "SISTEMA PAUSADO";
                    }
                }
                if(document.getElementById('candle-timer')) {
                    atualizarCronometroCandle(data.timeframe || 5);
                }
                if(document.getElementById('btn-telegram-toggle')) {
                    const ativo = !!data.telegram_ativo;
                    const btn = document.getElementById('btn-telegram-toggle');
                    btn.innerText = ativo ? "📡 TELEGRAM: ATIVO" : "📡 TELEGRAM: DESATIVADO";
                    btn.style.borderColor = ativo ? "#10b981" : "#ef4444";
                    btn.style.color = ativo ? "#10b981" : "#ef4444";
                }

                if(data.notificacao && data.notificacao.id !== lastNotifId) {
                    lastNotifId = data.notificacao.id;
                    dispararNotificacaoNativa(data.notificacao.titulo, data.notificacao.corpo, data.notificacao.id);
                }

                let histHtml = "";
                if(data.historico) {
                    data.historico.forEach(item => {
                        let cor = "#64748b";
                        if(item.res.includes("Win")) cor = "#10b981";
                        if(item.res.includes("Red")) cor = "#ef4444";
                        histHtml += `<div class="historico-item"><span>🕒 ${item.sinal}</span><b style="color:${cor}">${item.res}</b></div>`;
                    });
                }
                if(document.getElementById('lista-sinais')) document.getElementById('lista-sinais').innerHTML = histHtml || "<div style='text-align:center; font-size:11px; color:#64748b;'>Nenhum sinal no histórico.</div>";
            } catch (err) {
                console.warn('Falha ao atualizar o painel:', err);
            } finally {
                // Nova consulta 250ms após a resposta, sem acumular requisições.
                setTimeout(atualizarPainel, 1000);
            }
        }

        atualizarPainel();

        window.addEventListener('load', () => {
            if (window.Notification && Notification.permission === 'granted') {
                document.getElementById('btn-enable-notify').innerText = "✅ NOTIFICAÇÕES NATIVAS ATIVADAS";
                document.getElementById('btn-enable-notify').style.borderColor = "#10b981";
                document.getElementById('btn-enable-notify').style.color = "#10b981";
            }
        });
    </script>
</body>
</html>
"""

# ================= BANCO DE DADOS =================
def init_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                email VARCHAR(255) PRIMARY KEY,
                senha VARCHAR(255) NOT NULL,
                criado_em VARCHAR(50) NOT NULL,
                wins INT DEFAULT 0,
                reds INT DEFAULT 0,
                winrate FLOAT DEFAULT 0.0,
                ips_autorizados VARCHAR(255) DEFAULT '[]'
            );
            
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS ips_autorizados VARCHAR(255) DEFAULT '[]';
            ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS bloqueado BOOLEAN DEFAULT FALSE;

            CREATE TABLE IF NOT EXISTS historico_sinais (
                id SERIAL PRIMARY KEY,
                user_email VARCHAR(255) NOT NULL,
                sinal VARCHAR(255) NOT NULL,
                resultado VARCHAR(50) NOT NULL,
                ativo VARCHAR(100),
                direcao VARCHAR(10),
                timeframe INT,
                estrategia VARCHAR(100),
                score INT,
                mercado VARCHAR(50),
                contexto_timeframe VARCHAR(20),
                criado_em TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                resultado_em TIMESTAMPTZ
            );

            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS ativo VARCHAR(100);
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS direcao VARCHAR(10);
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS timeframe INT;
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS estrategia VARCHAR(100);
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS score INT;
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS mercado VARCHAR(50);
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS contexto_timeframe VARCHAR(20);
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS criado_em TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;
            ALTER TABLE historico_sinais ADD COLUMN IF NOT EXISTS resultado_em TIMESTAMPTZ;

            CREATE TABLE IF NOT EXISTS configuracoes_sistema (
                chave VARCHAR(100) PRIMARY KEY,
                valor VARCHAR(50) NOT NULL
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Aviso de Inicialização DB: {e}")

try:
    init_db()
except Exception:
    pass

def garantir_admin_configurado():
    """Cria/atualiza o ADM somente quando ADMIN_PASSWORD foi explicitamente configurada no ambiente."""
    if not ADMIN_PASSWORD:
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        senha_hash = generate_password_hash(ADMIN_PASSWORD)
        hoje = agora_brasilia().strftime("%Y-%m-%d")
        cur.execute("""
            INSERT INTO usuarios (email, senha, criado_em, wins, reds, winrate, ips_autorizados)
            VALUES (%s, %s, %s, 0, 0, 0.0, '[]')
            ON CONFLICT (email) DO UPDATE SET senha = EXCLUDED.senha;
        """, (ADMIN_EMAIL, senha_hash, hoje))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Aviso: não foi possível garantir a conta ADM: {e}")

garantir_admin_configurado()

def telegram_envio_ativo():
    """Retorna se o envio automático ao Telegram está habilitado pelo ADM."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT valor FROM configuracoes_sistema WHERE chave = 'telegram_ativo' LIMIT 1;")
        res = cur.fetchone()
        cur.close()
        conn.close()
        return str(res["valor"]).lower() == "true" if res else False
    except Exception:
        return False


def definir_telegram_ativo(ativo):
    """Altera o estado global do envio automático do Telegram. Somente o ADM chama esta função."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO configuracoes_sistema (chave, valor)
            VALUES ('telegram_ativo', %s)
            ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor;
        """, ("true" if ativo else "false",))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"⚠️ Erro ao salvar configuração do Telegram: {e}")
        return False


def parse_ips(ips_raw):
    try:
        if not ips_raw: return []
        return json.loads(ips_raw)
    except Exception:
        return []

def carregar_usuarios():
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM usuarios;")
        raw_data = cur.fetchall()
        cur.close()
        conn.close()

        dict_usuarios = {}
        for u in raw_data:
            email = u.get("email", "").strip().lower()
            if email:
                u_dict = dict(u)
                ips_list = parse_ips(u_dict.get("ips_autorizados", "[]"))
                u_dict["ips_list"] = ips_list
                u_dict["ips_Formatados"] = ", ".join(ips_list) if ips_list else "Nenhum (Livre)"
                dict_usuarios[email] = u_dict
        return dict_usuarios
    except Exception:
        return {}

def salvar_usuario(email, senha, data=None, ip_inicial=None):
    try:
        email_clean = email.strip().lower()
        data_criacao = data if data else agora_brasilia().strftime("%Y-%m-%d")
        senha_hash = senha if senha.startswith("scrypt:") or senha.startswith("pbkdf2:") else generate_password_hash(senha)
        
        ips = json.dumps([ip_inicial]) if ip_inicial else "[]"

        conn = get_db_connection()
        cur = conn.cursor()
        query = """
            INSERT INTO usuarios (email, senha, criado_em, wins, reds, winrate, ips_autorizados, bloqueado)
            VALUES (%s, %s, %s, 0, 0, 0.0, %s, FALSE)
            ON CONFLICT (email) DO UPDATE 
            SET senha = EXCLUDED.senha;
        """
        cur.execute(query, (email_clean, senha_hash, data_criacao, ips))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        raise e

def adicionar_ip_usuario(email, ip_cliente):
    try:
        email_clean = email.strip().lower()
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT ips_autorizados FROM usuarios WHERE email = %s;", (email_clean,))
        res = cur.fetchone()
        
        ips_list = parse_ips(res.get("ips_autorizados", "[]")) if res else []
        if ip_cliente not in ips_list and len(ips_list) < 2:
            ips_list.append(ip_cliente)
            cur.execute("UPDATE usuarios SET ips_autorizados = %s WHERE email = %s;", (json.dumps(ips_list), email_clean))
            conn.commit()
            
        cur.close()
        conn.close()
    except Exception:
        pass

def liberar_ip_usuario_db(email):
    try:
        email_clean = email.strip().lower()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET ips_autorizados = %s WHERE email = %s;", ("[]", email_clean))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

def atualizar_estatisticas_usuario(email, is_win):
    try:
        email_clean = email.strip().lower()
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT wins, reds FROM usuarios WHERE email = %s;", (email_clean,))
        res = cur.fetchone()

        if res:
            wins = res.get("wins", 0) + (1 if is_win else 0)
            reds = res.get("reds", 0) + (0 if is_win else 1)
            total = wins + reds
            winrate = round((wins / total) * 100, 1) if total > 0 else 0.0

            cur.execute("""
                UPDATE usuarios 
                SET wins = %s, reds = %s, winrate = %s 
                WHERE email = %s;
            """, (wins, reds, winrate, email_clean))
            conn.commit()

        cur.close()
        conn.close()
    except Exception:
        pass

def obter_estatisticas_usuario(email):
    """Retorna um snapshot das estatísticas atuais do usuário."""
    try:
        email_clean = email.strip().lower()
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT wins, reds, winrate FROM usuarios WHERE email = %s;", (email_clean,))
        res = cur.fetchone()
        cur.close()
        conn.close()
        if res:
            wins = int(res.get("wins") or 0)
            reds = int(res.get("reds") or 0)
            total = wins + reds
            winrate = round((wins / total) * 100, 1) if total else 0.0
            return {"wins": wins, "reds": reds, "total": total, "winrate": winrate}
    except Exception as e:
        print(f"⚠️ Erro ao obter estatísticas de {email}: {e}")
    return {"wins": 0, "reds": 0, "total": 0, "winrate": 0.0}


def zerar_estatisticas_usuario(email):
    try:
        email_clean = email.strip().lower()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET wins = 0, reds = 0, winrate = 0.0 WHERE email = %s;", (email_clean,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

def bloquear_usuario_db(email):
    try:
        email_clean = email.strip().lower()
        if email_clean == ADMIN_EMAIL:
            return False
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT COALESCE(bloqueado, FALSE) AS bloqueado FROM usuarios WHERE email = %s;", (email_clean,))
        res = cur.fetchone()
        if not res:
            cur.close()
            conn.close()
            return False
        novo_estado = not bool(res.get("bloqueado", False))
        cur.execute("UPDATE usuarios SET bloqueado = %s WHERE email = %s;", (novo_estado, email_clean))
        conn.commit()
        cur.close()
        conn.close()
        return novo_estado
    except Exception as e:
        print(f"⚠️ Erro ao bloquear/desbloquear usuário: {e}")
        return False

def renovar_usuario_db(email):
    try:
        hoje = agora_brasilia().strftime("%Y-%m-%d")
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE usuarios SET criado_em = %s WHERE email = %s;", (hoje, email.strip().lower()))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

def excluir_usuario_db(email):
    try:
        email_clean = email.strip().lower()
        if email_clean != ADMIN_EMAIL:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM usuarios WHERE email = %s;", (email_clean,))
            conn.commit()
            cur.close()
            conn.close()
    except Exception:
        pass

def verificar_assinatura(email):
    email_clean = email.strip().lower()
    if email_clean == ADMIN_EMAIL: return True, 999
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT criado_em FROM usuarios WHERE email = %s;", (email_clean,))
        res = cur.fetchone()
        cur.close()
        conn.close()

        if not res: return False, 0
        
        criado_str = str(res["criado_em"]).split("T")[0]
        data_criacao = datetime.strptime(criado_str, "%Y-%m-%d")
        dias_restantes = 30 - (agora_brasilia().replace(tzinfo=None) - data_criacao).days
        return (True, dias_restantes) if dias_restantes > 0 else (False, 0)
    except Exception:
        return True, 30

def registrar_sinal_bd(email, sinal_str, ativo=None, direcao=None, timeframe=None, estrategia=None, score=None, mercado=None, contexto_timeframe=None):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO historico_sinais
            (user_email, sinal, resultado, ativo, direcao, timeframe, estrategia, score, mercado, contexto_timeframe)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """, (email.strip().lower(), sinal_str, "Analisando...", ativo, direcao, timeframe, estrategia, score, mercado, contexto_timeframe))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ Erro ao registrar sinal: {e}")

def buscar_historico_bd(email):
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, sinal, resultado 
            FROM historico_sinais 
            WHERE user_email = %s 
            ORDER BY id DESC LIMIT 20;
        """, (email.strip().lower(),))
        res = cur.fetchall()
        cur.close()
        conn.close()
        return [{"id": r["id"], "sinal": r["sinal"], "res": r["resultado"]} for r in res]
    except Exception:
        return []

def atualizar_ultimo_sinal_bd(email, resultado):
    try:
        email_clean = email.strip().lower()
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id FROM historico_sinais 
            WHERE user_email = %s 
            ORDER BY id DESC LIMIT 1;
        """, (email_clean,))
        res = cur.fetchone()

        if res:
            ultimo_id = res["id"]
            cur.execute("UPDATE historico_sinais SET resultado = %s, resultado_em = CURRENT_TIMESTAMP WHERE id = %s;", (resultado, ultimo_id))
            conn.commit()

        cur.close()
        conn.close()
    except Exception:
        pass

# ================= BOT CONFIGS & ESTRATÉGIAS =================
LISTA_ESTRATEGIAS = ["PRICE_ACTION", "LOGICA_DO_PRECO", "RSI_MACD_MA", "MHI1", "REVERSAO"]

NOME_ESTRATEGIAS_DISPLAY = {
    "LOGICA_DO_PRECO": "Lógica do Preço",
    "RSI_MACD_MA": "RSI + Cruzamento MACD + MA",
    "MHI1": "MHI 1 (+ Filtro Tendência)",
    "REVERSAO": "Reversão de Bandas",
    "PRICE_ACTION": "Price Action Profissional",
    "TODAS": "Análise Dinâmica Múltipla"
}

# Peso relativo usado SOMENTE para desempatar sinais com a mesma probabilidade.
# A porcentagem continua sendo o primeiro critério; em empate, confluência e
# força da estratégia ajudam a decidir se um novo ativo realmente merece
# substituir o alerta atual. Esses pesos são configuráveis.
FORCA_ESTRATEGIA = {
    "PRICE_ACTION": 5,
    "RSI_MACD_MA": 4,
    "LOGICA_DO_PRECO": 3,
    "REVERSAO": 2,
    "MHI1": 1,
}

def forca_estrategia(nome):
    return FORCA_ESTRATEGIA.get(nome, 0)

# ================= ATIVOS DIVIDIDOS ABERTO E OTC =================
ATIVOS_BASE = {
    "FOREX_ABERTO": [
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
        "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "EURAUD", "EURCAD", "EURCHF"
    ],
    "CRIPTO_ABERTO": [
        "BTCUSD", "ETHUSD", "SOLUSD", "BNBUSD", "XRPUSD", "ADAUSD", "AVAXUSD",
        "LINKUSD", "DOGEUSD", "DOTUSD", "MATICUSD", "LTCUSD", "SHIBUSD", "TRXUSD"
    ],
    "FOREX_OTC": [
        "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCAD-OTC", "USDCHF-OTC", "NZDUSD-OTC",
        "EURGBP-OTC", "EURJPY-OTC", "GBPJPY-OTC", "AUDJPY-OTC", "EURAUD-OTC", "EURCAD-OTC", "EURCHF-OTC"
    ],
    "CRIPTO_OTC": [
        "BTCUSD-OTC", "ETHUSD-OTC", "SOLUSD-OTC", "BNBUSD-OTC", "XRPUSD-OTC", "ADAUSD-OTC", "AVAXUSD-OTC",
        "LINKUSD-OTC", "DOGEUSD-OTC", "DOTUSD-OTC", "MATICUSD-OTC", "LTCUSD-OTC", "SHIBUSD-OTC", "TRXUSD-OTC"
    ]
}

# ================= MAPEAMENTO DE TICKERS =================
MAPA_TICKERS = {}
for par in ATIVOS_BASE["FOREX_ABERTO"]: MAPA_TICKERS[par] = par + "=X"
for par in ATIVOS_BASE["CRIPTO_ABERTO"]: MAPA_TICKERS[par] = par.replace("USD", "-USD")
for par in ATIVOS_BASE["FOREX_OTC"]: MAPA_TICKERS[par] = par.replace("-OTC", "=X")
for par in ATIVOS_BASE["CRIPTO_OTC"]: MAPA_TICKERS[par] = par.replace("-OTC", "").replace("USD", "-USD")

# Ativos com fonte de preço real disponível para operação ao vivo.
# OTC não entra nesta lista enquanto não houver uma fonte OTC verificável.
ATIVOS_OPERAVEIS = list(dict.fromkeys(ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"] + ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]))

# ================= GRUPOS DE SELEÇÃO DE ATIVOS =================
GRUPOS_ATIVOS = {
    "TODOS": ATIVOS_OPERAVEIS,
    "FOREX": ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["FOREX_OTC"],
    "CRIPTO": ATIVOS_BASE["CRIPTO_ABERTO"] + ATIVOS_BASE["CRIPTO_OTC"],
    "ABERTOS": ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"],
    "OTC": ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"],
    "FOREX_ABERTO": ATIVOS_BASE["FOREX_ABERTO"],
    "FOREX_OTC": ATIVOS_BASE["FOREX_OTC"],
    "CRIPTO_ABERTO": ATIVOS_BASE["CRIPTO_ABERTO"],
    "CRIPTO_OTC": ATIVOS_BASE["CRIPTO_OTC"],
}

def normalizar_selecao_ativos(selecao):
    """Normaliza presets e ativos individuais para uma lista segura e sem duplicatas."""
    if not isinstance(selecao, (list, tuple, set)):
        selecao = [selecao]
    valores = [str(v).strip().upper() for v in selecao if str(v).strip()]
    if not valores or "TODOS" in valores:
        return ["TODOS"]
    resultado = []
    for valor in valores:
        if valor in GRUPOS_ATIVOS:
            for ativo in GRUPOS_ATIVOS[valor]:
                if ativo not in resultado:
                    resultado.append(ativo)
        elif valor in ATIVOS_OPERAVEIS and valor not in resultado:
            resultado.append(valor)
    return resultado or ["TODOS"]

def resumo_selecao_ativos(selecao):
    selecao = normalizar_selecao_ativos(selecao)
    if selecao == ["TODOS"]:
        return "TODOS"
    if len(selecao) == 1:
        return selecao[0]
    return f"{len(selecao)} ATIVOS SELECIONADOS"

def mercado_equivalente_selecao(selecao):
    """Retorna o filtro de mercado correspondente quando a seleção coincide com um grupo."""
    selecao = normalizar_selecao_ativos(selecao)
    if selecao == ["TODOS"]:
        return "TODOS"
    alvo = set(selecao)
    candidatos = ("ABERTO_TODOS", "OTC_TODOS", "FOREX_ABERTO", "FOREX_OTC", "CRIPTO_ABERTO", "CRIPTO_OTC")
    for mercado, grupo in [("ABERTO_TODOS", GRUPOS_ATIVOS["ABERTOS"]), ("OTC_TODOS", GRUPOS_ATIVOS["OTC"]), ("FOREX_ABERTO", GRUPOS_ATIVOS["FOREX_ABERTO"]), ("FOREX_OTC", GRUPOS_ATIVOS["FOREX_OTC"]), ("CRIPTO_ABERTO", GRUPOS_ATIVOS["CRIPTO_ABERTO"]), ("CRIPTO_OTC", GRUPOS_ATIVOS["CRIPTO_OTC"])]:
        if alvo == set(grupo):
            return mercado
    return "TODOS"

# ================= MOTOR DE ANÁLISE REAL DE 30 VELAS =================
def validar_ohlc(ohlc, velas_minimas=30, tf=5):
    """Valida integridade e atualidade das velas antes de entregá-las ao motor."""
    try:
        required = ("time", "open", "high", "low", "close")
        if any(k not in ohlc for k in required):
            return None
        arrays = {k: np.asarray(ohlc[k], dtype=float) for k in required}
        n = len(arrays["close"])
        if n < velas_minimas:
            return None
        if any(len(arrays[k]) != n for k in required):
            return None
        if any(not np.all(np.isfinite(arrays[k])) for k in required):
            return None
        if not np.all(np.diff(arrays["time"]) > 0):
            return None
        if np.any(arrays["high"] < np.maximum(arrays["open"], arrays["close"])):
            return None
        if np.any(arrays["low"] > np.minimum(arrays["open"], arrays["close"])):
            return None

        # Não analisa a vela ainda em formação: evita repaint e sinais baseados
        # em uma cotação que ainda pode mudar até o fechamento.
        agora_ts = int(time.time())
        limite = tf * 60
        timestamps = arrays["time"].astype(np.int64)
        fechado = (timestamps + limite) <= agora_ts
        if not np.any(fechado):
            return None
        ultimo_fechado = int(np.where(fechado)[0][-1])
        arrays = {k: arrays[k][:ultimo_fechado + 1] for k in required}
        if len(arrays["close"]) < velas_minimas:
            return None
        return arrays
    except Exception:
        return None

def _intervalo_binance(tf):
    return {1: "1m", 5: "5m", 15: "15m"}.get(int(tf))


def _simbolo_binance(ticker):
    """Converte o ticker interno BTC-USD para o par spot equivalente BTCUSDT."""
    base = str(ticker or "").upper().strip()
    if not base.endswith("-USD"):
        return None
    ativo = base[:-4].replace("-", "")
    if not ativo or not re.fullmatch(r"[A-Z0-9]+", ativo):
        return None
    return f"{ativo}USDT"


def _buscar_binance(ticker, tf, velas_minimas):
    """Busca candles públicos da Binance. Não exige API key para market data."""
    intervalo = _intervalo_binance(tf)
    simbolo = _simbolo_binance(ticker)
    if not intervalo or not simbolo:
        return None

    url = "https://data-api.binance.vision/api/v3/klines"
    try:
        res = requests.get(
            url,
            params={"symbol": simbolo, "interval": intervalo, "limit": max(100, min(1000, velas_minimas + 20))},
            headers={"User-Agent": "Vision-Trade-PRO-V3"},
            timeout=7.0
        )
        if res.status_code != 200:
            print(f"⚠️ Binance HTTP {res.status_code} para {simbolo} M{tf}.")
            return None

        payload = res.json()
        if not isinstance(payload, list) or not payload:
            return None

        ohlc = {
            "time": np.array([row[0] / 1000 for row in payload], dtype=float),
            "open": np.array([row[1] for row in payload], dtype=float),
            "high": np.array([row[2] for row in payload], dtype=float),
            "low": np.array([row[3] for row in payload], dtype=float),
            "close": np.array([row[4] for row in payload], dtype=float)
        }
        return validar_ohlc(ohlc, velas_minimas=velas_minimas, tf=tf)
    except Exception as e:
        print(f"⚠️ Binance indisponível para {simbolo} M{tf}: {e}")
        return None


def _simbolo_twelve_data(ticker):
    """Converte EURUSD=X para EUR/USD, formato aceito pela Twelve Data."""
    base = str(ticker or "").upper().strip()
    if not base.endswith("=X"):
        return None
    par = base[:-2].replace("/", "")
    if len(par) != 6 or not re.fullmatch(r"[A-Z]{6}", par):
        return None
    return f"{par[:3]}/{par[3:]}"


def _buscar_twelve_data(ticker, tf, velas_minimas):
    """Busca candles Forex reais pela Twelve Data usando a chave do Render."""
    api_key = obter_twelve_data_api_key()
    if not api_key:
        return None

    simbolo = _simbolo_twelve_data(ticker)
    intervalo = {1: "1min", 5: "5min", 15: "15min"}.get(int(tf))
    if not simbolo or not intervalo:
        return None

    url = "https://api.twelvedata.com/time_series"
    try:
        res = requests.get(
            url,
            params={
                "symbol": simbolo,
                "interval": intervalo,
                "outputsize": max(100, min(5000, velas_minimas + 30)),
                "timezone": "UTC",
                "order": "asc",
                "apikey": api_key
            },
            headers={"User-Agent": "Vision-Trade-PRO-V3"},
            timeout=7.0
        )
        if res.status_code != 200:
            try:
                erro_api = res.json().get("message", res.text[:300])
            except Exception:
                erro_api = res.text[:300]
            print(f"⚠️ Twelve Data HTTP {res.status_code} para {simbolo} M{tf}: {erro_api}")
            return None

        payload = res.json()
        if not isinstance(payload, dict) or payload.get("status") == "error":
            mensagem = payload.get("message", "resposta inválida") if isinstance(payload, dict) else "resposta inválida"
            print(f"⚠️ Twelve Data recusou {simbolo} M{tf}: {mensagem}")
            return None

        valores = payload.get("values") or []
        if not valores:
            return None

        # A Twelve Data normalmente retorna as séries mais recentes primeiro.
        # Ordenamos pelo horário para entregar ao motor exatamente o mesmo
        # formato cronológico usado pelas demais fontes.
        registros = []
        for item in valores:
            try:
                dt = datetime.strptime(str(item["datetime"]), "%Y-%m-%d %H:%M:%S")
                dt = pytz.UTC.localize(dt)
                registros.append((
                    dt.timestamp(),
                    float(item["open"]),
                    float(item["high"]),
                    float(item["low"]),
                    float(item["close"])
                ))
            except (KeyError, TypeError, ValueError):
                continue

        registros.sort(key=lambda x: x[0])
        if not registros:
            return None

        ohlc = {
            "time": np.array([x[0] for x in registros], dtype=float),
            "open": np.array([x[1] for x in registros], dtype=float),
            "high": np.array([x[2] for x in registros], dtype=float),
            "low": np.array([x[3] for x in registros], dtype=float),
            "close": np.array([x[4] for x in registros], dtype=float)
        }
        return validar_ohlc(ohlc, velas_minimas=velas_minimas, tf=tf)
    except Exception as e:
        print(f"⚠️ Twelve Data indisponível para {simbolo} M{tf}: {e}")
        return None



# ================= CONECTOR DE CANDLES OTC DA QUOTEX =================
class QuotexOTCFeed:
    """
    Mantém uma conexão assíncrona com a Quotex e fornece candles OTC
    para o motor síncrono do Flask. Somente leitura; não executa ordens.

    A conexão tenta hosts suportados pelo PyQuotex em sequência. Isso é
    importante quando o host padrão sofre bloqueio regional/Cloudflare.
    """
    DEFAULT_HOSTS = (
        "qxbroker.com",
        "quotex.com",
        "qxbroker.io",
        "quotex.io",
        "qxbroker.sqldb.tc",
    )

    def __init__(self):
        self._loop = None
        self._thread = None
        self._client = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._last_error = ""
        self._started = False
        self._active_host = ""

    def _start_loop(self):
        if self._started and self._thread and self._thread.is_alive():
            return
        self._started = True

        def runner():
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._ready.set()
                self._loop.run_forever()
            except Exception as e:
                self._last_error = f"Loop Quotex encerrado: {e}"
                self._ready.set()

        self._thread = threading.Thread(
            target=runner,
            name="quotex-otc-loop",
            daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5)

    @staticmethod
    def _asset_to_quotex(ticker):
        base = str(ticker or "").upper().strip()
        if not base.endswith("-OTC"):
            return None
        return base[:-4] + "_otc"

    @classmethod
    def _hosts(cls):
        """Retorna os hosts em ordem, sem duplicatas."""
        raw = QUOTEX_HOSTS
        if raw:
            candidatos = [x.strip().lower() for x in raw.split(",") if x.strip()]
        elif QUOTEX_HOST:
            candidatos = [QUOTEX_HOST]
        else:
            candidatos = list(cls.DEFAULT_HOSTS)

        hosts = []
        for host in candidatos:
            host = host.replace("https://", "").replace("http://", "").strip("/")
            if host and host not in hosts:
                hosts.append(host)
        return hosts or list(cls.DEFAULT_HOSTS)

    @staticmethod
    def _format_connection_error(error):
        """Evita mensagens quebradas por incompatibilidades de atributos.
        Algumas camadas de WebSocket expõem status_code, outras apenas texto.
        """
        if error is None:
            return "erro desconhecido"

        parts = []
        for attr in ("status_code", "status", "code"):
            try:
                value = getattr(error, attr, None)
                if value not in (None, "") and str(value) not in parts:
                    parts.append(str(value))
            except Exception:
                pass

        try:
            message = str(error).strip()
        except Exception:
            message = repr(error)

        # Evita exibir a exceção secundária 'reason_phrase' como se fosse
        # a causa original do bloqueio.
        if "reason_phrase" in message and not parts:
            message = "Resposta HTTP rejeitada pelo servidor WebSocket (detalhes indisponíveis)."

        if parts and message:
            return f"HTTP/status {'/'.join(parts)} — {message}"
        return message or "erro sem mensagem"

    @staticmethod
    def _is_rejection(reason):
        text = str(reason or "").lower()
        return any(token in text for token in (
            "403", "forbidden", "rejected", "cloudflare", "handshake"
        ))

    async def _close_async(self):
        client = self._client
        self._client = None
        self._active_host = ""
        if client:
            try:
                await client.close()
            except Exception:
                pass

    async def _connect_async(self):
        if self._client is not None:
            try:
                if await self._client.check_connect():
                    return True
            except Exception:
                pass
            await self._close_async()

        if not QUOTEX_EMAIL or not QUOTEX_PASSWORD:
            self._last_error = (
                "QUOTEX_EMAIL e QUOTEX_PASSWORD não configuradas no Render."
            )
            return False

        try:
            from pyquotex.stable_api import Quotex
        except Exception as e:
            self._last_error = (
                "Biblioteca pyquotex não instalada no Render. "
                f"Erro: {self._format_connection_error(e)}"
            )
            return False

        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        )

        erros = []
        hosts = self._hosts()

        for host in hosts:
            try:
                print(f"🔌 Quotex OTC: tentando host {host}...")
                self._client = Quotex(
                    email=QUOTEX_EMAIL,
                    password=QUOTEX_PASSWORD,
                    host=host,
                    lang="pt",
                    user_agent=ua,
                )

                # SSID é opcional. Quando não existe, o PyQuotex executa
                # o fluxo normal de autenticação/login.
                if QUOTEX_SSID:
                    try:
                        self._client.set_session(ua, ssid=QUOTEX_SSID)
                    except Exception as e:
                        print(
                            "⚠️ Quotex: não foi possível aplicar QUOTEX_SSID; "
                            f"seguindo com diagnóstico do host {host}: "
                            f"{self._format_connection_error(e)}"
                        )

                ok, motivo = await self._client.connect()
                if ok:
                    self._active_host = host
                    self._last_error = ""
                    print(f"✅ Quotex OTC: conexão estabelecida via {host}.")
                    return True

                motivo_txt = self._format_connection_error(motivo)
                erros.append(f"{host}: {motivo_txt}")
                print(f"⚠️ Quotex OTC: {host} rejeitou a conexão: {motivo_txt}")
                await self._close_async()

                # 403/handshake/Cloudflare: tenta o próximo host.
                # Outros erros também recebem tentativa no próximo host,
                # pois o endpoint alternativo pode usar outra rota/região.
                continue

            except Exception as e:
                motivo_txt = self._format_connection_error(e)
                erros.append(f"{host}: {motivo_txt}")
                print(f"⚠️ Quotex OTC: erro no host {host}: {motivo_txt}")
                await self._close_async()

        resumo = " | ".join(erros[-len(hosts):])
        if resumo:
            self._last_error = (
                "Quotex recusou a conexão em todos os hosts testados. "
                f"Diagnóstico: {resumo}"
            )
        else:
            self._last_error = "Não foi possível estabelecer conexão com a Quotex."
        return False

    @staticmethod
    def _normalizar_candles(candles, tf, velas_minimas):
        agora = time.time()
        registros = []

        if not isinstance(candles, (list, tuple)):
            return None

        for item in candles:
            if not isinstance(item, dict):
                continue
            try:
                ts = float(item.get("time"))
                o = float(item.get("open"))
                h = float(item.get("high"))
                l = float(item.get("low"))
                c = float(item.get("close"))
                if not all(math.isfinite(x) for x in (ts, o, h, l, c)):
                    continue
                # Somente candles fechados entram na análise.
                if ts + (int(tf) * 60) > agora:
                    continue
                registros.append((ts, o, h, l, c))
            except (TypeError, ValueError):
                continue

        if not registros:
            return None

        registros.sort(key=lambda x: x[0])
        unicos = {}
        for row in registros:
            unicos[row[0]] = row
        registros = sorted(unicos.values(), key=lambda x: x[0])

        ohlc = {
            "time": np.array([x[0] for x in registros], dtype=float),
            "open": np.array([x[1] for x in registros], dtype=float),
            "high": np.array([x[2] for x in registros], dtype=float),
            "low": np.array([x[3] for x in registros], dtype=float),
            "close": np.array([x[4] for x in registros], dtype=float)
        }
        return validar_ohlc(ohlc, velas_minimas=velas_minimas, tf=tf)

    async def _get_async(self, ticker, tf, velas_minimas):
        asset = self._asset_to_quotex(ticker)
        if not asset:
            return None

        if not await self._connect_async():
            return None

        periodo = {1: 60, 5: 300, 15: 900}.get(int(tf))
        if not periodo:
            self._last_error = f"Timeframe M{tf} não suportado pela fonte Quotex."
            return None

        try:
            # O endpoint WebSocket da Quotex entrega as velas históricas.
            # O retorno atual é suficiente para o motor que exige 100 velas.
            candles = await self._client.get_candles(
                asset=asset,
                end_from_time=time.time(),
                offset=periodo * 200,
                period=periodo
            )
            dados = self._normalizar_candles(candles, tf, velas_minimas)
            if dados is None:
                self._last_error = (
                    f"Quotex não retornou candles OTC fechados suficientes "
                    f"para {asset} M{tf}."
                )
            return dados
        except Exception as e:
            self._last_error = (
                f"Erro ao buscar {asset} M{tf} na Quotex "
                f"({self._active_host or 'host desconhecido'}): "
                f"{self._format_connection_error(e)}"
            )
            try:
                await self._close_async()
            except Exception:
                pass
            return None

    def get(self, ticker, tf, velas_minimas=100):
        if "-OTC" not in str(ticker).upper():
            return None

        self._start_loop()
        if not self._loop or not self._thread or not self._thread.is_alive():
            self._last_error = "Loop de conexão Quotex não iniciou."
            return None

        with self._lock:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._get_async(ticker, tf, velas_minimas),
                    self._loop
                )
                return future.result(timeout=60)
            except Exception as e:
                self._last_error = (
                    "Timeout/erro no feed Quotex: "
                    f"{self._format_connection_error(e)}"
                )
                return None

    def diagnostico(self):
        if self._last_error:
            return self._last_error
        if not QUOTEX_EMAIL or not QUOTEX_PASSWORD:
            return "QUOTEX_EMAIL/QUOTEX_PASSWORD não configuradas."
        if self._active_host:
            return f"Quotex OTC conectada via {self._active_host}."
        return "Quotex OTC pronta para conexão."

QUOTEX_OTC_FEED = QuotexOTCFeed()

def get_data_v2(ticker, tf, velas_minimas=100):
    """Busca somente OHLC verificável das fontes correspondentes ao mercado."""
    if not ticker or not tf:
        return None

    try:
        base_ticker = str(ticker).upper().strip()

        # ================= OTC: QUOTEX =================
        if base_ticker.endswith("-OTC"):
            return QUOTEX_OTC_FEED.get(base_ticker, tf, velas_minimas=velas_minimas)

        # ================= CRIPTO: BINANCE =================
        if base_ticker.endswith("-USD"):
            return _buscar_binance(base_ticker, tf, velas_minimas)

        # ================= FOREX: TWELVE DATA =================
        if base_ticker.endswith("=X"):
            return _buscar_twelve_data(base_ticker, tf, velas_minimas)

        return None
    except Exception as e:
        print(f"⚠️ Fonte de mercado indisponível para {ticker} M{tf}: {e}")
        return None


def calcular_ema(dados, periodo):
    if len(dados) < periodo:
        return np.array(dados)
    ema = np.zeros_like(dados)
    multiplicador = 2 / (periodo + 1)
    ema[periodo-1] = np.mean(dados[:periodo])
    for i in range(periodo, len(dados)):
        ema[i] = (dados[i] - ema[i-1]) * multiplicador + ema[i-1]
    return ema

# ================= PRICE ACTION PROFISSIONAL =================
def analisar_price_action(data, i=-1):
    """
    Price Action baseado somente em leitura de preço: estrutura, rejeição,
    engolfo e localização em suporte/resistência recente.
    Exige pelo menos 3 confirmações independentes.
    """
    c, o, h, l = data["close"], data["open"], data["high"], data["low"]
    if len(c) < 30:
        return None, 0, 0

    idx = i if i >= 0 else len(c) - 1
    if idx < 4:
        return None, 0, 0

    corpo = abs(c[idx] - o[idx])
    amplitude = max(h[idx] - l[idx], 1e-9)
    pavio_sup = h[idx] - max(o[idx], c[idx])
    pavio_inf = min(o[idx], c[idx]) - l[idx]

    hh_hl = h[idx] > h[idx-2] and l[idx] > l[idx-2]
    lh_ll = h[idx] < h[idx-2] and l[idx] < l[idx-2]

    suporte = float(np.min(l[max(0, idx-12):idx]))
    resistencia = float(np.max(h[max(0, idx-12):idx]))
    faixa_media = float(np.mean(h[max(0, idx-12):idx]) - np.mean(l[max(0, idx-12):idx]))
    zona = max(faixa_media * 0.20, amplitude * 0.35)

    perto_suporte = abs(l[idx] - suporte) <= zona or abs(c[idx] - suporte) <= zona
    perto_resistencia = abs(h[idx] - resistencia) <= zona or abs(c[idx] - resistencia) <= zona

    pin_call = pavio_inf >= amplitude * 0.55 and corpo <= amplitude * 0.40 and c[idx] > o[idx]
    pin_put = pavio_sup >= amplitude * 0.55 and corpo <= amplitude * 0.40 and c[idx] < o[idx]

    prev_body = abs(c[idx-1] - o[idx-1])
    engulf_call = c[idx] > o[idx] and c[idx-1] < o[idx-1] and c[idx] >= o[idx-1] and o[idx] <= c[idx-1] and corpo >= prev_body * 1.05
    engulf_put = c[idx] < o[idx] and c[idx-1] > o[idx-1] and o[idx] >= c[idx-1] and c[idx] <= o[idx-1] and corpo >= prev_body * 1.05

    call_conf = sum([bool(pin_call or engulf_call), bool(perto_suporte), bool(hh_hl), bool(c[idx] > o[idx])])
    put_conf = sum([bool(pin_put or engulf_put), bool(perto_resistencia), bool(lh_ll), bool(c[idx] < o[idx])])

    if call_conf >= 3 and call_conf > put_conf:
        return "CALL", min(96, 78 + call_conf * 5), call_conf
    if put_conf >= 3 and put_conf > call_conf:
        return "PUT", min(96, 78 + put_conf * 5), put_conf
    return None, 0, max(call_conf, put_conf)


# ================= CONTEXTO MULTI-TIMEFRAME =================
def timeframe_contexto(tf):
    return {1: 5, 5: 15, 15: 30}.get(int(tf), 30)

def obter_contexto_tendencia(data):
    try:
        c = np.asarray(data["close"], dtype=float)
        if len(c) < 60:
            return "NEUTRO"
        ema20 = calcular_ema(c, 20)
        ema50 = calcular_ema(c, 50)
        ultimo = c[-1]
        inclinacao = ema20[-1] - ema20[-5]
        if ultimo > ema20[-1] > ema50[-1] and inclinacao > 0:
            return "CALL"
        if ultimo < ema20[-1] < ema50[-1] and inclinacao < 0:
            return "PUT"
        return "NEUTRO"
    except Exception:
        return "NEUTRO"

def validar_contexto_multitimeframe(ticker, tf, sinal, cache):
    """Exige alinhamento do timeframe superior quando o contexto é claro."""
    superior = timeframe_contexto(tf)
    chave = f"{ticker}_{superior}"
    if chave in cache:
        data_sup = cache[chave].get("data")
    else:
        data_sup = get_data_v2(ticker, superior, velas_minimas=100)
        cache[chave] = {"data": data_sup, "time": time.time()}
    if data_sup is None:
        return False, "SEM_CONTEXTO"
    tendencia = obter_contexto_tendencia(data_sup)
    if tendencia == "NEUTRO":
        return True, "NEUTRO"
    return tendencia == sinal, tendencia

# ================= ESTATÍSTICAS HISTÓRICAS =================
def _estatisticas_agregadas(rows):
    """Normaliza uma lista de agregações SQL e calcula a taxa observada."""
    saida = []
    for row in rows:
        total = int(row.get("total") or 0)
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        winrate = round((wins / total) * 100, 2) if total else 0.0
        item = dict(row)
        item.update({"total": total, "wins": wins, "losses": losses, "winrate": winrate})
        saida.append(item)
    return saida


def consultar_estatisticas_sinais(ativo=None, timeframe=None, estrategia=None, contexto=None, score_min=None, score_max=None):
    """Consulta resultados reais registrados no PostgreSQL, sem misturar sinais ainda sem resultado."""
    filtros = ["resultado IN ('Win', 'WinG1', 'Red')"]
    params = []

    if ativo:
        filtros.append("ativo = %s")
        params.append(ativo)
    if timeframe:
        filtros.append("timeframe = %s")
        params.append(int(timeframe))
    if estrategia:
        filtros.append("estrategia = %s")
        params.append(estrategia)
    if contexto:
        filtros.append("contexto_timeframe = %s")
        params.append(contexto)
    if score_min is not None:
        filtros.append("score >= %s")
        params.append(int(score_min))
    if score_max is not None:
        filtros.append("score <= %s")
        params.append(int(score_max))

    where = " AND ".join(filtros)

    def executar(cur, sql, extra_params=None):
        cur.execute(sql.format(where=where), tuple(params + (extra_params or [])))
        return cur.fetchall()

    resultado = {
        "resumo": {"total": 0, "wins": 0, "losses": 0, "winrate": 0.0, "score_medio": 0.0},
        "por_ativo": [],
        "por_timeframe": [],
        "por_estrategia": [],
        "por_horario": [],
        "por_score": [],
        "por_contexto": [],
        "por_combinacao": []
    }

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE resultado IN ('Win', 'WinG1')) AS wins,
                COUNT(*) FILTER (WHERE resultado = 'Red') AS losses,
                COALESCE(AVG(score) FILTER (WHERE score IS NOT NULL), 0) AS score_medio
            FROM historico_sinais
            WHERE {where};
        """, tuple(params))
        resumo = cur.fetchone() or {}
        total = int(resumo.get("total") or 0)
        wins = int(resumo.get("wins") or 0)
        losses = int(resumo.get("losses") or 0)
        resultado["resumo"] = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "winrate": round((wins / total) * 100, 2) if total else 0.0,
            "score_medio": round(float(resumo.get("score_medio") or 0), 2)
        }

        resultado["por_ativo"] = _estatisticas_agregadas(executar(cur, """
            SELECT COALESCE(ativo, 'N/D') AS grupo,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE resultado IN ('Win', 'WinG1')) AS wins,
                   COUNT(*) FILTER (WHERE resultado = 'Red') AS losses,
                   COALESCE(AVG(score), 0) AS score_medio
            FROM historico_sinais WHERE {where}
            GROUP BY COALESCE(ativo, 'N/D') ORDER BY total DESC, grupo ASC;
        """))

        resultado["por_timeframe"] = _estatisticas_agregadas(executar(cur, """
            SELECT COALESCE(timeframe, 0) AS grupo,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE resultado IN ('Win', 'WinG1')) AS wins,
                   COUNT(*) FILTER (WHERE resultado = 'Red') AS losses,
                   COALESCE(AVG(score), 0) AS score_medio
            FROM historico_sinais WHERE {where}
            GROUP BY COALESCE(timeframe, 0) ORDER BY grupo ASC;
        """))

        resultado["por_estrategia"] = _estatisticas_agregadas(executar(cur, """
            SELECT COALESCE(estrategia, 'N/D') AS grupo,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE resultado IN ('Win', 'WinG1')) AS wins,
                   COUNT(*) FILTER (WHERE resultado = 'Red') AS losses,
                   COALESCE(AVG(score), 0) AS score_medio
            FROM historico_sinais WHERE {where}
            GROUP BY COALESCE(estrategia, 'N/D') ORDER BY total DESC, grupo ASC;
        """))

        resultado["por_horario"] = _estatisticas_agregadas(executar(cur, """
            SELECT EXTRACT(HOUR FROM (criado_em AT TIME ZONE 'America/Sao_Paulo'))::INT AS grupo,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE resultado IN ('Win', 'WinG1')) AS wins,
                   COUNT(*) FILTER (WHERE resultado = 'Red') AS losses,
                   COALESCE(AVG(score), 0) AS score_medio
            FROM historico_sinais WHERE {where}
            GROUP BY EXTRACT(HOUR FROM (criado_em AT TIME ZONE 'America/Sao_Paulo'))
            ORDER BY grupo ASC;
        """))

        resultado["por_score"] = _estatisticas_agregadas(executar(cur, """
            SELECT CASE
                       WHEN score IS NULL THEN 'SEM SCORE'
                       WHEN score < 60 THEN '0-59'
                       WHEN score < 70 THEN '60-69'
                       WHEN score < 80 THEN '70-79'
                       WHEN score < 90 THEN '80-89'
                       ELSE '90-100'
                   END AS grupo,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE resultado IN ('Win', 'WinG1')) AS wins,
                   COUNT(*) FILTER (WHERE resultado = 'Red') AS losses,
                   COALESCE(AVG(score), 0) AS score_medio
            FROM historico_sinais WHERE {where}
            GROUP BY 1
            ORDER BY MIN(score) NULLS LAST;
        """))

        resultado["por_contexto"] = _estatisticas_agregadas(executar(cur, """
            SELECT COALESCE(contexto_timeframe, 'N/D') AS grupo,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE resultado IN ('Win', 'WinG1')) AS wins,
                   COUNT(*) FILTER (WHERE resultado = 'Red') AS losses,
                   COALESCE(AVG(score), 0) AS score_medio
            FROM historico_sinais WHERE {where}
            GROUP BY COALESCE(contexto_timeframe, 'N/D') ORDER BY total DESC, grupo ASC;
        """))

        resultado["por_combinacao"] = _estatisticas_agregadas(executar(cur, """
            SELECT COALESCE(ativo, 'N/D') AS ativo,
                   COALESCE(timeframe, 0) AS timeframe,
                   COALESCE(estrategia, 'N/D') AS estrategia,
                   COALESCE(contexto_timeframe, 'N/D') AS contexto,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE resultado IN ('Win', 'WinG1')) AS wins,
                   COUNT(*) FILTER (WHERE resultado = 'Red') AS losses,
                   COALESCE(AVG(score), 0) AS score_medio
            FROM historico_sinais WHERE {where}
            GROUP BY COALESCE(ativo, 'N/D'), COALESCE(timeframe, 0),
                     COALESCE(estrategia, 'N/D'), COALESCE(contexto_timeframe, 'N/D')
            ORDER BY total DESC, (COUNT(*) FILTER (WHERE resultado IN ('Win', 'WinG1'))::NUMERIC / NULLIF(COUNT(*), 0)) DESC NULLS LAST
            LIMIT 100;
        """))

        for grupo in (resultado["por_ativo"], resultado["por_timeframe"], resultado["por_estrategia"], resultado["por_horario"], resultado["por_score"], resultado["por_contexto"], resultado["por_combinacao"]):
            for item in grupo:
                if "score_medio" in item:
                    item["score_medio"] = round(float(item.get("score_medio") or 0), 2)

        return resultado
    except Exception as e:
        print(f"⚠️ Erro ao consultar estatísticas históricas: {e}")
        resultado["erro"] = str(e)
        return resultado
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        try:
            if conn:
                conn.close()
        except Exception:
            pass


# ================= BACKTEST HISTÓRICO =================
def backtest_estrategia(data, estrategia, tf, expiracao_velas=1, modo_gale="SEM_GALE"):
    """Backtest walk-forward com opção SEM_GALE ou GALE1, sem olhar candles futuros."""
    arrays = {k: np.asarray(data[k]) for k in ("time", "open", "high", "low", "close")}
    c = arrays["close"].astype(float)
    modo_gale = str(modo_gale or "SEM_GALE").upper()
    if modo_gale not in {"SEM_GALE", "GALE1"}:
        modo_gale = "SEM_GALE"

    total = wins = wins_g1 = losses = draws = 0
    scores = []
    i = 30
    limite = len(c) - max(1, int(expiracao_velas))

    while i < limite:
        historico = {k: v[:i + 1] for k, v in arrays.items()}
        sinal, score = analisar_estrategia(historico, estrategia, i=-1)
        if not sinal or not score:
            i += 1
            continue

        total += 1
        scores.append(float(score))
        preco_entrada = float(c[i])
        preco_saida = float(c[i + expiracao_velas])

        if preco_saida == preco_entrada:
            draws += 1
            i += 1
            continue

        ganhou = (sinal == "CALL" and preco_saida > preco_entrada) or (sinal == "PUT" and preco_saida < preco_entrada)
        if ganhou:
            wins += 1
            i += 1
            continue

        if modo_gale == "GALE1":
            indice_g1 = i + expiracao_velas
            indice_saida_g1 = indice_g1 + expiracao_velas
            if indice_saida_g1 < len(c):
                preco_entrada_g1 = float(c[indice_g1])
                preco_saida_g1 = float(c[indice_saida_g1])
                if preco_saida_g1 == preco_entrada_g1:
                    draws += 1
                else:
                    ganhou_g1 = (sinal == "CALL" and preco_saida_g1 > preco_entrada_g1) or (sinal == "PUT" and preco_saida_g1 < preco_entrada_g1)
                    if ganhou_g1:
                        wins_g1 += 1
                    else:
                        losses += 1
                i = indice_saida_g1 + 1
                continue

        losses += 1
        i += 1

    avaliados = wins + wins_g1 + losses
    taxa = round(((wins + wins_g1) / avaliados) * 100, 2) if avaliados else 0.0
    return {
        "estrategia": estrategia,
        "timeframe": tf,
        "modo_gale": modo_gale,
        "total": total,
        "wins": wins,
        "wins_g1": wins_g1,
        "losses": losses,
        "draws": draws,
        "avaliados": avaliados,
        "winrate": taxa,
        "score_medio": round(float(np.mean(scores)), 2) if scores else 0.0
    }

def nome_fonte_ativo(ativo_nome):
    ativo_nome = str(ativo_nome or "").upper()
    if ativo_nome.endswith("-OTC"):
        return "Quotex OTC"
    if ativo_nome in ATIVOS_BASE["CRIPTO_ABERTO"]:
        return "Binance"
    return "Twelve Data"

# Jobs de backtest executados em background para nunca bloquear o bot em tempo real.
BACKTEST_JOBS = {}
BACKTEST_JOBS_LOCK = threading.Lock()
BACKTEST_JOB_TTL = 1800
BACKTEST_ACTIVE_JOB_ID = None
BACKTEST_CANCEL_EVENTS = {}
BACKTEST_MAX_RESULT_LINES = 100

def _atualizar_backtest_job(job_id, **updates):
    with BACKTEST_JOBS_LOCK:
        job = BACKTEST_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = time.time()

def _limpar_backtest_jobs():
    limite = time.time() - BACKTEST_JOB_TTL
    with BACKTEST_JOBS_LOCK:
        antigos = [k for k, v in BACKTEST_JOBS.items() if v.get("updated_at", 0) < limite]
        for k in antigos:
            BACKTEST_JOBS.pop(k, None)
            BACKTEST_CANCEL_EVENTS.pop(k, None)

def _cancelar_backtest_anterior():
    """Cancela qualquer backtest anterior para impedir jobs concorrentes e acúmulo de RAM."""
    global BACKTEST_ACTIVE_JOB_ID
    with BACKTEST_JOBS_LOCK:
        anterior = BACKTEST_ACTIVE_JOB_ID
        if anterior:
            evento = BACKTEST_CANCEL_EVENTS.get(anterior)
            if evento:
                evento.set()
            job = BACKTEST_JOBS.get(anterior)
            if job and job.get("status") == "running":
                job.update({
                    "status": "cancelled",
                    "percentual": min(100, int(job.get("percentual", 0))),
                    "mensagem": "Backtest anterior cancelado para liberar recursos.",
                    "etapa": "CANCELADO",
                    "updated_at": time.time()
                })
        BACKTEST_ACTIVE_JOB_ID = None
        # Mantém no dicionário apenas o necessário para a tela atual.
        for job_id in list(BACKTEST_JOBS.keys()):
            if job_id != anterior and BACKTEST_JOBS[job_id].get("status") in {"done", "error", "cancelled"}:
                BACKTEST_JOBS.pop(job_id, None)
                BACKTEST_CANCEL_EVENTS.pop(job_id, None)

def _backtest_cancelado(job_id):
    with BACKTEST_JOBS_LOCK:
        evento = BACKTEST_CANCEL_EVENTS.get(job_id)
        return bool(evento and evento.is_set())

def executar_backtest_real(mercado, ativo, tf_selecionado, estrategia_selecionada, modo_gale="SEM_GALE", progress_callback=None, cancel_check=None):
    """Executa a análise somente com fontes reais. Sem banco de resultados e sem dados sintéticos.
    progress_callback é opcional e permite acompanhar a análise sem bloquear a interface."""
    mercado = str(mercado or "ABERTO").upper()
    ativo = str(ativo or "TODOS").upper()
    tf_selecionado = str(tf_selecionado or "TODOS")
    estrategia_selecionada = str(estrategia_selecionada or "TODAS").upper()
    modo_gale = str(modo_gale or "SEM_GALE").upper()
    if modo_gale not in {"SEM_GALE", "GALE1"}: modo_gale = "SEM_GALE"

    abertos = ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"]
    if mercado == "OTC":
        candidatos = ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]
    elif mercado == "AMBOS":
        candidatos = abertos + ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]
    else:
        candidatos = abertos
    if ativo != "TODOS":
        candidatos = [a for a in candidatos if a == ativo]
    candidatos = list(dict.fromkeys(candidatos))
    tfs = [1,5,15] if tf_selecionado == "TODOS" else [int(tf_selecionado)]
    estrategias = LISTA_ESTRATEGIAS if estrategia_selecionada == "TODAS" else [estrategia_selecionada]

    linhas = []
    fontes_indisponiveis = []
    cache = {}
    unidades_totais = max(1, len(candidatos) * len(tfs))
    unidades_concluidas = 0
    if progress_callback:
        try:
            progress_callback(0, unidades_totais, "Preparando dados reais...", "INICIANDO")
        except Exception:
            pass
    for ativo_nome in candidatos:
        if cancel_check and cancel_check():
            return {"cancelado": True, "erro": "Backtest cancelado para liberar recursos.", "linhas": [], "melhores_por_ativo": []}
        is_otc = "-OTC" in ativo_nome.upper()
        ticker = ativo_nome if is_otc else MAPA_TICKERS.get(ativo_nome)
        fonte = nome_fonte_ativo(ativo_nome)
        for tf in tfs:
            if cancel_check and cancel_check():
                return {"cancelado": True, "erro": "Backtest cancelado para liberar recursos.", "linhas": [], "melhores_por_ativo": []}
            unidades_concluidas += 1
            if progress_callback:
                try:
                    progress_callback(
                        unidades_concluidas - 1,
                        unidades_totais,
                        f"Obtendo candles reais: {ativo_nome} M{tf}",
                        "DADOS"
                    )
                except Exception:
                    pass
            chave = (ticker, tf)
            if chave not in cache:
                cache[chave] = get_data_v2(ticker, tf, velas_minimas=100)
            data = cache[chave]
            if data is None:
                if fonte == "Twelve Data" and not obter_twelve_data_api_key():
                    motivo = "Chave da Twelve Data não configurada no Render. Use TWELVE_DATA_API_KEY ou TWELVEDATA_API_KEY."
                elif fonte == "Twelve Data":
                    motivo = "Twelve Data não retornou candles válidos; verifique créditos/limite da API e a chave configurada"
                elif fonte == "Quotex OTC":
                    motivo = QUOTEX_OTC_FEED.diagnostico()
                else:
                    motivo = "fonte indisponível ou sem candles fechados suficientes"
                fontes_indisponiveis.append(f"{ativo_nome} M{tf}: {motivo}")
                continue
            for estrategia in estrategias:
                r = backtest_estrategia(data, estrategia, tf, expiracao_velas=1, modo_gale=modo_gale)
                if r["total"] == 0:
                    continue
                linhas.append({"mercado":("OTC" if is_otc else "ABERTO"), "ativo":ativo_nome, "fonte":fonte, "tf":tf, "estrategia":estrategia, "estrategia_nome":NOME_ESTRATEGIAS_DISPLAY.get(estrategia, estrategia), **r})
            if progress_callback:
                try:
                    progress_callback(
                        unidades_concluidas,
                        unidades_totais,
                        f"Analisado: {ativo_nome} M{tf}",
                        "ANALISE"
                    )
                except Exception:
                    pass

    linhas.sort(key=lambda x: (x["winrate"], x["avaliados"], x["score_medio"]), reverse=True)
    melhores = {}
    for r in linhas:
        chave = (r["ativo"], r["tf"])
        if chave not in melhores:
            melhores[chave] = r
    melhores_por_ativo = sorted(melhores.values(), key=lambda x: (x["ativo"], x["tf"]))
    total_sinais = sum(r["total"] for r in linhas)
    total_wins = sum(r["wins"] for r in linhas)
    total_wins_g1 = sum(r.get("wins_g1", 0) for r in linhas)
    total_losses = sum(r["losses"] for r in linhas)
    avaliados = total_wins + total_wins_g1 + total_losses
    taxa = round((total_wins + total_wins_g1) / avaliados * 100, 2) if avaliados else 0.0
    return {
        "combinacoes": len(linhas), "sinais": total_sinais, "wins": total_wins, "wins_g1": total_wins_g1, "losses": total_losses,
        "winrate": taxa, "melhor_taxa": linhas[0]["winrate"] if linhas else 0.0, "modo_gale": modo_gale,
        "linhas": linhas[:BACKTEST_MAX_RESULT_LINES], "melhores_por_ativo": melhores_por_ativo[:BACKTEST_MAX_RESULT_LINES],
        "erro": "; ".join(fontes_indisponiveis[:8]) if fontes_indisponiveis else ""
    }

# ================= MOTOR DE ESTRATÉGIAS COM SCORE TÉCNICO =================
def analisar_estrategia(data, estrategia, i=-1):
    c, o, h, l = data["close"], data["open"], data["high"], data["low"]
    
    if len(c) < 30: 
        return None, 0
        
    sinal = None
    probabilidade = 0

    if estrategia == "PRICE_ACTION":
        sinal, probabilidade, _ = analisar_price_action(data, i)
        return sinal, probabilidade

    if estrategia == "LOGICA_DO_PRECO":
        tamanho = abs(c[i] - o[i])
        amplitude = h[i] - l[i]
        if amplitude > 0 and tamanho > 0:
            cor = "G" if c[i] > o[i] else "R"
            p_sup = h[i] - max(o[i], c[i])
            p_inf = min(o[i], c[i]) - l[i]
            
            # Rejeição de Fundo / Suporte
            if cor == "G" and p_inf >= (amplitude * 0.45) and p_sup <= (amplitude * 0.20):
                sinal = "CALL"
                probabilidade = int(82 + (p_inf / amplitude) * 15)
            # Rejeição de Topo / Resistência
            elif cor == "R" and p_sup >= (amplitude * 0.45) and p_inf <= (amplitude * 0.20):
                sinal = "PUT"
                probabilidade = int(82 + (p_sup / amplitude) * 15)
            # Exaustão Compradora
            elif cor == "G" and p_sup >= (amplitude * 0.50) and tamanho <= (amplitude * 0.35):
                sinal = "PUT"
                probabilidade = int(80 + (p_sup / amplitude) * 15)
            # Exaustão Vendedora
            elif cor == "R" and p_inf >= (amplitude * 0.50) and tamanho <= (amplitude * 0.35):
                sinal = "CALL"
                probabilidade = int(80 + (p_inf / amplitude) * 15)

    elif estrategia == "RSI_MACD_MA":
        if len(c) >= 26:
            diff = np.diff(c[-15:])
            gains = diff[diff > 0]
            losses = np.abs(diff[diff < 0])
            avg_gain = np.mean(gains) if len(gains) > 0 else 1e-7
            avg_loss = np.mean(losses) if len(losses) > 0 else 1e-7
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

            ema12 = calcular_ema(c, 12)
            ema26 = calcular_ema(c, 26)
            macd_line = ema12 - ema26
            signal_line = calcular_ema(macd_line, 9)

            if rsi <= 35 and macd_line[i] > signal_line[i]:
                sinal = "CALL"
                probabilidade = int(83 + (35 - rsi) * 0.5)
            elif rsi >= 65 and macd_line[i] < signal_line[i]:
                sinal = "PUT"
                probabilidade = int(83 + (rsi - 65) * 0.5)

    elif estrategia == "MHI1":
        cores = []
        for j in range(i-2, i+1):
            if c[j] > o[j]: cores.append("G")
            elif c[j] < o[j]: cores.append("R")
            else: cores.append("D") 
            
        if "D" not in cores:
            qtd_g = cores.count("G")
            qtd_r = cores.count("R")
            
            ema20 = np.mean(c[-20:])
            if qtd_g == 2 and qtd_r == 1 and c[i] <= ema20:
                sinal = "PUT"
                probabilidade = 84
            elif qtd_r == 2 and qtd_g == 1 and c[i] >= ema20:
                sinal = "CALL"
                probabilidade = 84
            elif qtd_g == 3:
                sinal = "PUT"
                probabilidade = 88
            elif qtd_r == 3:
                sinal = "CALL"
                probabilidade = 88

    elif estrategia in ["REVERSAO", "RETRACAO"]:
        std = np.std(c[-20:])
        ma = np.mean(c[-20:])
        banda_superior = ma + (2.0 * std)
        banda_inferior = ma - (2.0 * std)

        if c[i] <= banda_inferior and c[i] < o[i]: 
            sinal = "CALL"
            dist = (banda_inferior - c[i]) / (std if std > 0 else 1)
            probabilidade = int(81 + min(15, dist * 10))
        elif c[i] >= banda_superior and c[i] > o[i]: 
            sinal = "PUT"
            dist = (c[i] - banda_superior) / (std if std > 0 else 1)
            probabilidade = int(81 + min(15, dist * 10))

    probabilidade = min(98, max(75, probabilidade)) if sinal else 0
    return sinal, probabilidade

# ================= ROTA SERVICE WORKER DE NOTIFICAÇÃO =================
@app.route('/sw.js')
def service_worker():
    sw_code = """
    self.addEventListener('install', function(event) {
        self.skipWaiting();
    });

    self.addEventListener('activate', function(event) {
        event.waitUntil(self.clients.claim());
    });

    self.addEventListener('notificationclick', function(event) {
        event.notification.close();

        event.waitUntil(
            clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
                for (var i = 0; i < clientList.length; i++) {
                    var client = clientList[i];
                    if ('focus' in client) return client.focus();
                }
                if (clients.openWindow) return clients.openWindow('/');
            })
        );
    });
    """
    response = Response(sw_code, mimetype='application/javascript')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# ================= ROTAS DE NAVEGAÇÃO =================
@app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        e = request.form.get('email', '').strip().lower()
        s = request.form.get('password', '').strip()
        ip_cliente = get_client_ip()

        if not e or not s:
            return render_template_string(HTML_LOGIN, erro="Preencha todos os campos.")

        chave_login = f"{get_client_ip()}|{e}"
        if login_bloqueado(chave_login):
            return render_template_string(HTML_LOGIN, erro="Muitas tentativas. Aguarde alguns minutos antes de tentar novamente.")

        usuarios = carregar_usuarios()
        if e not in usuarios:
            return render_template_string(HTML_LOGIN, erro=f"Usuário não cadastrado ({e}). Faça o cadastro.")

        user_db = usuarios[e]
        if not check_password_hash(user_db['senha'], s):
            registrar_falha_login(chave_login)
            return render_template_string(HTML_LOGIN, erro="Senha Incorreta.")

        limpar_falhas_login(chave_login)

        if e != ADMIN_EMAIL and bool(user_db.get('bloqueado', False)):
            return render_template_string(HTML_LOGIN, erro="🚫 USUÁRIO BLOQUEADO PELO ADMINISTRADOR.")

        if e != ADMIN_EMAIL:
            ips_cadastrados = user_db.get('ips_list', [])
            if ip_cliente not in ips_cadastrados:
                if len(ips_cadastrados) < 2:
                    adicionar_ip_usuario(e, ip_cliente)
                else:
                    return render_template_string(HTML_LOGIN, erro="🚫 ACESSO BLOQUEADO: Limite de 2 IPs/dispositivos atingido.")

        ativo, dias = verificar_assinatura(e)
        if not ativo:
            return render_template_string(HTML_LOGIN, erro=f"Assinatura expirada (Dias: {dias}).")

        session.clear()
        session['user'] = e
        session.permanent = True
        USUARIOS_ONLINE[e] = time.time()
        get_user_state(e)
        return redirect('/')

    return render_template_string(HTML_LOGIN)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        e = request.form.get('email', '').strip().lower()
        s = request.form.get('password', '').strip()
        ip_cliente = get_client_ip()
        
        if not e or not s:
            return render_template_string(HTML_REGISTER, erro="Preencha todos os campos.")

        if len(s) < 8:
            return render_template_string(HTML_REGISTER, erro="A senha deve ter pelo menos 8 caracteres.")

        if e == ADMIN_EMAIL:
            return render_template_string(HTML_REGISTER, erro="Este e-mail é reservado ao administrador.")
            
        try:
            salvar_usuario(e, s, ip_inicial=ip_cliente)
            session.clear()
            session['user'] = e
            session.permanent = True
            USUARIOS_ONLINE[e] = time.time()
            get_user_state(e)
            return redirect('/')
        except Exception as err:
            return render_template_string(HTML_REGISTER, erro=f"Erro ao salvar: {err}")

    return render_template_string(HTML_REGISTER)

@app.route('/logout')
def logout():
    user = session.get('user')
    if user in USUARIOS_ONLINE: del USUARIOS_ONLINE[user]
    session.clear()
    return redirect('/login')

@app.route('/termos')
def termos():
    return render_template_string(HTML_TERMOS)

@app.route('/admin_panel')
def admin_panel():
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    now = time.time()
    for u in list(USUARIOS_ONLINE.keys()):
        if now - USUARIOS_ONLINE[u] > 60: del USUARIOS_ONLINE[u]
    return render_template_string(HTML_ADM, lista=carregar_usuarios(), admin=ADMIN_EMAIL, online_count=len(USUARIOS_ONLINE), online_list=USUARIOS_ONLINE.keys())

@app.route('/admin/backtest/status')
def admin_backtest_status():
    if session.get('user') != ADMIN_EMAIL:
        return abort(403)
    job_id = request.args.get('job', '').strip()
    _limpar_backtest_jobs()
    with BACKTEST_JOBS_LOCK:
        job = dict(BACKTEST_JOBS.get(job_id, {})) if job_id else {}
    if not job:
        return jsonify({"ok": False, "status": "not_found", "error": "Análise não encontrada ou expirada."}), 404
    resultado = job.get("resultado")
    payload = {
        "ok": True,
        "status": job.get("status", "running"),
        "percentual": job.get("percentual", 0),
        "concluidas": job.get("concluidas", 0),
        "total": job.get("total", 0),
        "mensagem": job.get("mensagem", "Processando..."),
        "etapa": job.get("etapa", "INICIANDO"),
        "resultado": resultado,
        "erro": job.get("erro", "")
    }
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

def _rodar_backtest_em_background(job_id, mercado, ativo, tf, estrategia, modo_gale):
    global BACKTEST_ACTIVE_JOB_ID
    try:
        def progresso(concluidas, total, mensagem, etapa):
            percentual = int(min(99, round((concluidas / max(1, total)) * 100)))
            _atualizar_backtest_job(
                job_id, status="running", percentual=percentual,
                concluidas=concluidas, total=total, mensagem=mensagem, etapa=etapa
            )

        resultado = executar_backtest_real(
            mercado, ativo, tf, estrategia, modo_gale,
            progress_callback=progresso,
            cancel_check=lambda: _backtest_cancelado(job_id)
        )
        if resultado.get("cancelado"):
            _atualizar_backtest_job(
                job_id, status="cancelled", percentual=100,
                mensagem=resultado.get("erro", "Backtest cancelado."),
                etapa="CANCELADO", resultado=None, erro=resultado.get("erro", "")
            )
            return
        _atualizar_backtest_job(
            job_id, status="done", percentual=100,
            mensagem="Análise concluída com dados reais.", etapa="CONCLUIDO",
            resultado=resultado, erro=""
        )
    except Exception as exc:
        logging.exception("Falha no backtest em background")
        _atualizar_backtest_job(
            job_id, status="error", percentual=100,
            mensagem="A análise foi interrompida por um erro.", etapa="ERRO",
            resultado=None, erro=f"Falha na análise real: {exc}"
        )
    finally:
        with BACKTEST_JOBS_LOCK:
            BACKTEST_CANCEL_EVENTS.pop(job_id, None)
            if BACKTEST_ACTIVE_JOB_ID == job_id:
                BACKTEST_ACTIVE_JOB_ID = None
        gc.collect()

@app.route('/admin/estatisticas')
def admin_estatisticas():
    if session.get('user') != ADMIN_EMAIL:
        return abort(403)

    ativos = list(dict.fromkeys(ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"] + ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]))
    estrategias = {k: NOME_ESTRATEGIAS_DISPLAY.get(k, k) for k in LISTA_ESTRATEGIAS}
    mercado = request.args.get('mercado', 'ABERTO').strip().upper()
    if mercado not in {'ABERTO','OTC','AMBOS'}: mercado = 'ABERTO'
    ativo = request.args.get('ativo', 'TODOS').strip().upper()
    if ativo != 'TODOS' and ativo not in ativos: ativo = 'TODOS'
    tf = request.args.get('tf', 'TODOS').strip()
    if tf not in {'TODOS','1','5','15'}: tf = 'TODOS'
    estrategia = request.args.get('estrategia', 'TODAS').strip().upper()
    if estrategia != 'TODAS' and estrategia not in LISTA_ESTRATEGIAS: estrategia = 'TODAS'
    modo_gale = request.args.get('gale', 'SEM_GALE').strip().upper()
    if modo_gale not in {'SEM_GALE', 'GALE1'}: modo_gale = 'SEM_GALE'

    resultado = None
    job_id = request.args.get('job', '').strip()
    job = None

    # Nunca executa o backtest pesado dentro da requisição HTTP.
    # Isso mantém o robô de análise em tempo real independente do backtest.
    if request.args.get('analisar') == '1' and not job_id:
        global BACKTEST_ACTIVE_JOB_ID
        _cancelar_backtest_anterior()
        _limpar_backtest_jobs()
        job_id = str(time.time_ns())
        cancel_event = threading.Event()
        with BACKTEST_JOBS_LOCK:
            BACKTEST_CANCEL_EVENTS[job_id] = cancel_event
            BACKTEST_ACTIVE_JOB_ID = job_id
            BACKTEST_JOBS[job_id] = {
                "status": "running",
                "percentual": 0,
                "concluidas": 0,
                "total": 0,
                "mensagem": "Preparando análise com dados reais...",
                "etapa": "INICIANDO",
                "resultado": None,
                "erro": "",
                "created_at": time.time(),
                "updated_at": time.time()
            }
        threading.Thread(
            target=_rodar_backtest_em_background,
            args=(job_id, mercado, ativo, tf, estrategia, modo_gale),
            daemon=True
        ).start()

    if job_id:
        with BACKTEST_JOBS_LOCK:
            job = dict(BACKTEST_JOBS.get(job_id, {}))
        if job and job.get("status") == "done":
            resultado = job.get("resultado")

    return render_template_string(HTML_ESTATISTICAS, filtros={'mercado':mercado,'ativo':ativo,'tf':tf,'estrategia':estrategia,'gale':modo_gale}, ativos=ativos, estrategias=estrategias, resultado=resultado, backtest_job=job, backtest_job_id=job_id)


@app.route('/adm/renovar/<email>', methods=['POST'])
def adm_renovar(email):
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    renovar_usuario_db(email)
    return redirect('/admin_panel')

@app.route('/adm/liberar_ip/<email>', methods=['POST'])
def adm_liberar_ip(email):
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    liberar_ip_usuario_db(email)
    return redirect('/admin_panel')

@app.route('/adm/editar', methods=['POST'])
def adm_editar():
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    original = request.form.get('email_original', '').strip().lower()
    novo_email = request.form.get('novo_email', '').strip().lower()
    nova_senha = request.form.get('nova_senha', '').strip()
    if nova_senha and len(nova_senha) < 8:
        return redirect('/admin_panel')
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        if nova_senha:
            hash_senha = generate_password_hash(nova_senha)
            cur.execute("UPDATE usuarios SET email = %s, senha = %s WHERE email = %s;", (novo_email, hash_senha, original))
        else:
            cur.execute("UPDATE usuarios SET email = %s WHERE email = %s;", (novo_email, original))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass
        
    return redirect('/admin_panel')

@app.route('/adm/bloquear/<email>', methods=['POST'])
def adm_bloquear(email):
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    bloquear_usuario_db(email)
    return redirect('/admin_panel')

@app.route('/adm/excluir/<email>', methods=['POST'])
def adm_excluir(email):
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    excluir_usuario_db(email)
    return redirect('/admin_panel')

def restaurar_estado_da_sessao(user, st):
    """Reidrata configuração do robô a partir da sessão quando o usuário
    retorna ao painel ou quando uma requisição chega a outro worker/processo.
    Não substitui um estado já carregado durante a vida normal do processo."""
    if not user or not st or st.get("_sessao_hidratada"):
        return st

    cfg = session.get("vision_bot_config") or {}
    if isinstance(cfg, dict):
        try:
            tf = int(cfg.get("timeframe", st.get("timeframe", 5)))
            if tf in (1, 5, 15):
                st["timeframe"] = tf
        except (TypeError, ValueError):
            pass

        mercado = str(cfg.get("tipo_mercado", st.get("tipo_mercado", "TODOS"))).upper()
        if mercado in {"TODOS", "ABERTO_TODOS", "OTC_TODOS", "FOREX_ABERTO", "FOREX_OTC", "CRIPTO_ABERTO", "CRIPTO_OTC"}:
            st["tipo_mercado"] = mercado

        selecao = cfg.get("ativos_selecionados", st.get("ativos_selecionados", ["TODOS"]))
        st["ativos_selecionados"] = normalizar_selecao_ativos(selecao)
        st["ativo_selecionado"] = resumo_selecao_ativos(st["ativos_selecionados"])

        estrategia = str(cfg.get("estrategia", st.get("estrategia", "TODAS"))).upper()
        if estrategia == "TODAS" or estrategia in LISTA_ESTRATEGIAS or "," in estrategia:
            st["estrategia"] = estrategia

        if bool(cfg.get("bot_iniciado", False)):
            st["bot_iniciado"] = True
            st["bot_pausado"] = bool(cfg.get("bot_pausado", False))
            if not st["bot_pausado"] and not st.get("inicio_varredura"):
                st["inicio_varredura"] = time.time() + 1

    st["_sessao_hidratada"] = True
    return st

def salvar_configuracao_sessao(st):
    """Persiste somente configuração/estado de execução não sensível na sessão."""
    session["vision_bot_config"] = {
        "timeframe": int(st.get("timeframe", 5)),
        "tipo_mercado": str(st.get("tipo_mercado", "TODOS")),
        "ativos_selecionados": list(st.get("ativos_selecionados", ["TODOS"])),
        "estrategia": str(st.get("estrategia", "TODAS")),
        "bot_iniciado": bool(st.get("bot_iniciado", False)),
        "bot_pausado": bool(st.get("bot_pausado", True))
    }

@app.route('/')
def index():
    if 'user' not in session: return redirect('/login')
    user = session['user']
    USUARIOS_ONLINE[user] = time.time()
    st = get_user_state(user)
    restaurar_estado_da_sessao(user, st)
    return render_template_string(HTML_INDEX, modo=st["tipo_mercado"], tf=st["timeframe"], estrat=st["estrategia"], ativo_selecionado=st.get("ativo_selecionado", "TODOS"), ativos_selecionados=st.get("ativos_selecionados", ["TODOS"]), resumo_ativos=resumo_selecao_ativos(st.get("ativos_selecionados", ["TODOS"])), user=user, admin=ADMIN_EMAIL, ATIVOS_BASE=ATIVOS_BASE, ATIVOS_OPERAVEIS=ATIVOS_OPERAVEIS, NOME_ESTRATEGIAS_DISPLAY=NOME_ESTRATEGIAS_DISPLAY, LISTA_ESTRATEGIAS=LISTA_ESTRATEGIAS)

@app.route('/status')
def status():
    user = session.get('user')
    if not user: return jsonify({})
    USUARIOS_ONLINE[user] = time.time()
    
    st = get_user_state(user)
    restaurar_estado_da_sessao(user, st)
    usuarios = carregar_usuarios()
    u_info = usuarios.get(user, {"wins": 0, "reds": 0, "winrate": 0.0})
    historico = buscar_historico_bd(user)
    
    display_texto = st["sinal_permanente"] if (st["aguardando_confirmacao"] and st["sinal_permanente"]) else st["ultimo_sinal"]

    response = jsonify({
        "html": display_texto, 
        "aguardando": st["aguardando_confirmacao"], 
        "wins": u_info.get("wins", 0),
        "reds": u_info.get("reds", 0), 
        "winrate": u_info.get("winrate", 0.0), 
        "historico": historico,
        "ativo_atual": st["ativo_atual"],
        "mercado": st["tipo_mercado"],
        "ativo_selecionado": st.get("ativo_selecionado", "TODOS"),
        "ativos_selecionados": st.get("ativos_selecionados", ["TODOS"]),
        "rodando": st["bot_iniciado"] and not st["bot_pausado"],
        "timeframe": st["timeframe"],
        "candle_decorrido": st.get("candle_decorrido", 0),
        "candle_restante": st.get("candle_restante", st["timeframe"] * 60),
        "telegram_ativo": telegram_envio_ativo() if user == ADMIN_EMAIL else False,
        "notificacao": st["notificacao"]
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

@app.route('/command/<cmd>', methods=['POST'])
def command(cmd):
    user = session.get('user')
    if not user:
        return jsonify({"ok": False})
    
    st = get_user_state(user)
    restaurar_estado_da_sessao(user, st)

    if cmd == "toggle_telegram":
        if user != ADMIN_EMAIL:
            return jsonify({"ok": False, "error": "Apenas o ADM pode alterar o Telegram."}), 403
        novo_estado = not telegram_envio_ativo()
        if definir_telegram_ativo(novo_estado):
            st["telegram_ativo"] = novo_estado
            st["ultimo_sinal"] = (
                "<div class='system-console' style='color:#10b981;'>"
                + ("📡 TELEGRAM ATIVADO PELO ADM." if novo_estado else "🔕 TELEGRAM DESATIVADO PELO ADM.")
                + "</div>"
            )
            return jsonify({"ok": True, "telegram_ativo": novo_estado})
        return jsonify({"ok": False, "error": "Não foi possível salvar a configuração."}), 500

    if cmd == "test_telegram":
        if user != ADMIN_EMAIL or not telegram_envio_ativo():
            return jsonify({"ok": False, "error": "O teste do Telegram é exclusivo do ADM e exige o envio ativado."}), 403
        msg_teste = (
            f"🧪 <b>TESTE DE COMUNICAÇÃO - VISION PRO V3</b>\n\n"
            f"✅ Conexão estabelecida com sucesso com o Telegram!\n"
            f"👤 Usuário: {user}\n"
            f"⏰ Horário: {agora_brasilia().strftime('%H:%M:%S')}"
        )
        diagnostico_ok, diagnostico_msg = diagnosticar_telegram()
        if not diagnostico_ok:
            st["ultimo_sinal"] = (
                "<div class='system-console' style='color:#ef4444;'>"
                f"❌ TELEGRAM NÃO CONFIGURADO/ACESSÍVEL.<br>{diagnostico_msg}"
                "</div>"
            )
            return jsonify({"ok": False, "error": diagnostico_msg}), 502

        msg_id = enviar_telegram(msg_teste, user_solicitante=user)
        if msg_id:
            st["ultimo_sinal"] = "<div class='system-console' style='color:#10b981;'>✅ MENSAGEM DE TESTE ENVIADA AO TELEGRAM COM SUCESSO!</div>"
            return jsonify({"ok": True, "message_id": msg_id, "diagnostico": diagnostico_msg})

        erro = "A API do Telegram foi validada, mas o envio da mensagem falhou. Veja os logs do Render."
        st["ultimo_sinal"] = f"<div class='system-console' style='color:#ef4444;'>❌ FALHA NO ENVIO TELEGRAM.<br>{erro}</div>"
        return jsonify({"ok": False, "error": erro, "diagnostico": diagnostico_msg}), 502

    elif cmd == "start_bot":
        # Cada START inicia uma nova sessão de estatísticas.
        zerar_estatisticas_usuario(user)
        st["sinal_confirmado_dados"] = None
        st["ultimo_resumo_sessao"] = None
        st["bot_iniciado"] = True
        st["bot_pausado"] = False
        st["aguardando_confirmacao"] = False
        st["sinal_permanente"] = None
        if st.get("timer_confirmacao"):
            try:
                st["timer_confirmacao"].cancel()
            except Exception:
                pass
        st["timer_confirmacao"] = None
        st["alerta_ativo"] = None
        st["inicio_varredura"] = time.time() + 2 
        st["sinais_enviados"].clear() 
        
        st["ativo_atual"] = "INICIANDO VARREDURA..."
        st["ultimo_sinal"] = f"<div class='system-console'>⚡ <b>INICIANDO MOTOR DE ANÁLISE DINÂMICA</b><br><span style='color:#00f2fe;'>[VARRENDO TODOS OS ATIVOS...]</span></div><div class='tech-scanner'></div>"
        salvar_configuracao_sessao(st)
        
        msg_inicio_telegram = (
            f"🚀 <b>SISTEMA VISION PRO V3 INICIADO</b>\n\n"
            f"🟢 <b>Status:</b> Análise de 30 velas ativada\n"
            f"👤 <b>Usuário:</b> {user}\n"
            f"📊 <b>Timeframe:</b> M{st['timeframe']}\n"
            f"🌐 <b>Mercado:</b> {st['tipo_mercado']}\n"
            f"💱 <b>Ativo:</b> {st.get('ativo_selecionado', 'TODOS')}\n"
            f"⚙️ <b>Estratégia:</b> {NOME_ESTRATEGIAS_DISPLAY.get(st['estrategia'], st['estrategia'])}\n\n"
            f"<i>Varrendo gráficos em tempo real...</i>"
        )
        enviar_telegram(msg_inicio_telegram, user_solicitante=user)
        return jsonify({"ok": True})

    elif cmd == "pause_bot":
        st["bot_pausado"] = not st["bot_pausado"]
        status_txt = "[PAUSADO] VARREDURA EM PAUSA..." if st["bot_pausado"] else f"🔍 ANALISANDO: {st['ativo_atual']} (M{st['timeframe']})"
        st["ultimo_sinal"] = f"<div class='system-console' style='color:#f59e0b;'>{status_txt}</div>" if st["bot_pausado"] else f"<div class='system-console'>🔍 ANALISANDO 30 VELAS: <b>{st['ativo_atual']}</b> (M{st['timeframe']})<br><span style='color:#00f2fe;'>[VARREDURA CONTINUA]</span></div><div class='tech-scanner'></div>"
        salvar_configuracao_sessao(st)
        msg_pause = "⏸ <b>SISTEMA PAUSADO</b>" if st["bot_pausado"] else "▶️ <b>SISTEMA RETOMADO!</b>"
        enviar_telegram(msg_pause, user_solicitante=user)
        return jsonify({"ok": True})

    elif cmd == "stop_bot":
        # Captura as estatísticas ANTES de encerrar a sessão.
        stats = obter_estatisticas_usuario(user)
        st["ultimo_resumo_sessao"] = stats.copy()

        st["bot_iniciado"] = False
        st["bot_pausado"] = True
        st["aguardando_confirmacao"] = False
        st["sinal_permanente"] = None
        if st.get("timer_confirmacao"):
            try:
                st["timer_confirmacao"].cancel()
            except Exception:
                pass
        st["timer_confirmacao"] = None

        alerta_para_apagar = st.get("alerta_ativo") or {}
        msg_alerta_id = alerta_para_apagar.get("msg_id")
        if msg_alerta_id:
            deletar_mensagem_telegram(msg_alerta_id)
        st["alerta_ativo"] = None
        st["ativo_atual"] = "DESCONECTADO"

        if stats["total"] > 0:
            emoji_desempenho = "🏆" if stats["winrate"] >= 70 else ("📊" if stats["winrate"] >= 50 else "⚠️")
            msg_encerramento = (
                f"🛑 <b>SESSÃO ENCERRADA — VISION PRO V3 ULTRA</b>\n\n"
                f"{emoji_desempenho} <b>RESUMO DA SESSÃO</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>Operações:</b> {stats['total']}\n"
                f"🟢 <b>WIN:</b> {stats['wins']}\n"
                f"🔴 <b>RED:</b> {stats['reds']}\n"
                f"📈 <b>Assertividade:</b> {stats['winrate']:.1f}%\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"🤖 O robô foi encerrado e a sessão de análise foi finalizada.\n"
                f"⚠️ <i>Os resultados são apenas o registro desta sessão e não representam garantia de resultados futuros.</i>"
            )
        else:
            msg_encerramento = (
                "🛑 <b>SESSÃO ENCERRADA — VISION PRO V3 ULTRA</b>\n\n"
                "📊 <b>Resumo da sessão:</b> nenhuma operação foi registrada.\n\n"
                "🤖 O robô foi encerrado com segurança."
            )

        st["ultimo_sinal"] = (
            f"<div class='system-console' style='color:#00f2fe;'>"
            f"🛑 <b>ROBÔ ENCERRADO</b><br>"
            f"Operações: {stats['total']} | WIN: {stats['wins']} | RED: {stats['reds']} | "
            f"Assertividade: {stats['winrate']:.1f}%"
            f"</div>"
        )
        salvar_configuracao_sessao(st)
        enviar_telegram(msg_encerramento, user_solicitante=user)
        return jsonify({"ok": True, "estatisticas": stats})

    elif cmd.startswith("tf_"):
        st["timeframe"] = int(cmd.split('_')[1])
        salvar_configuracao_sessao(st)
    elif cmd.startswith("mkt_"):
        st["tipo_mercado"] = cmd.split('_', 1)[1]
        # Sincroniza o seletor de mercado com os presets de ativos.
        preset_por_mercado = {
            "TODOS": "TODOS",
            "ABERTO_TODOS": "ABERTOS",
            "OTC_TODOS": "OTC",
            "FOREX_ABERTO": "FOREX_ABERTO",
            "FOREX_OTC": "FOREX_OTC",
            "CRIPTO_ABERTO": "CRIPTO_ABERTO",
            "CRIPTO_OTC": "CRIPTO_OTC"
        }
        preset = preset_por_mercado.get(st["tipo_mercado"], "TODOS")
        st["ativos_selecionados"] = normalizar_selecao_ativos([preset])
        st["ativo_selecionado"] = resumo_selecao_ativos(st["ativos_selecionados"])
        st["sinais_enviados"].clear()
        salvar_configuracao_sessao(st)
    elif cmd.startswith("ativos_"):
        bruto = cmd.replace("ativos_", "", 1)
        valores = [v.strip().upper() for v in bruto.split(',') if v.strip()]
        st["ativos_selecionados"] = normalizar_selecao_ativos(valores)
        st["tipo_mercado"] = mercado_equivalente_selecao(st["ativos_selecionados"])
        st["ativo_selecionado"] = resumo_selecao_ativos(st["ativos_selecionados"])
        st["sinais_enviados"].clear()
        # Ao trocar a seleção, invalida qualquer alerta anterior para impedir
        # que um sinal de um ativo antigo seja confirmado depois da troca.
        if st.get("timer_confirmacao"):
            try:
                st["timer_confirmacao"].cancel()
            except Exception:
                pass
        st["timer_confirmacao"] = None
        st["alerta_ativo"] = None
        st["aguardando_confirmacao"] = False
        st["sinal_permanente"] = None
        st["sinal_confirmado_dados"] = None
        st["ultimo_sinal"] = (
            f"<div class='system-console' style='color:#00f2fe;'>"
            f"🎯 SELEÇÃO DE ATIVOS: <b>{st['ativo_selecionado']}</b><br>"
            f"O robô analisará a seleção escolhida."
            f"</div>"
        )
        salvar_configuracao_sessao(st)
    elif cmd.startswith("ativo_"):
        # Compatibilidade com comandos antigos de seleção de um único ativo.
        ativo_escolhido = cmd.replace("ativo_", "", 1).upper()
        if ativo_escolhido == "TODOS" or ativo_escolhido in ATIVOS_OPERAVEIS:
            st["ativos_selecionados"] = normalizar_selecao_ativos([ativo_escolhido])
            st["ativo_selecionado"] = resumo_selecao_ativos(st["ativos_selecionados"])
            st["sinais_enviados"].clear()
            if st.get("timer_confirmacao"):
                try:
                    st["timer_confirmacao"].cancel()
                except Exception:
                    pass
            st["timer_confirmacao"] = None
            st["alerta_ativo"] = None
            st["aguardando_confirmacao"] = False
            st["sinal_permanente"] = None
            st["sinal_confirmado_dados"] = None
            st["ultimo_sinal"] = (
                f"<div class='system-console' style='color:#00f2fe;'>"
                f"🎯 ATIVO SELECIONADO: <b>{ativo_escolhido}</b><br>"
                f"O robô analisará somente este ativo."
                f"</div>"
            )
            salvar_configuracao_sessao(st)
    elif cmd.startswith("set_est_"):
        st["estrategia"] = cmd.replace("set_est_", "")
        salvar_configuracao_sessao(st)
    
    return jsonify({"ok": True})

@app.route('/admin/backtest')
def admin_backtest():
    if session.get('user') != ADMIN_EMAIL:
        return abort(403)
    ativo = request.args.get('ativo', 'EURUSD').strip().upper()
    try:
        tf = int(request.args.get('tf', '5'))
    except ValueError:
        return jsonify({"ok": False, "error": "Timeframe inválido."}), 400
    if tf not in (1, 5, 15):
        return jsonify({"ok": False, "error": "Timeframe permitido: M1, M5 ou M15."}), 400
    estrategia = request.args.get('estrategia', 'PRICE_ACTION').strip().upper()
    if estrategia not in LISTA_ESTRATEGIAS:
        return jsonify({"ok": False, "error": "Estratégia inválida."}), 400
    modo_gale = request.args.get('gale', 'SEM_GALE').strip().upper()
    if modo_gale not in {'SEM_GALE', 'GALE1'}:
        return jsonify({"ok": False, "error": "Modo de Gale inválido. Use SEM_GALE ou GALE1."}), 400
    ticker = ativo if "-OTC" in ativo else MAPA_TICKERS.get(ativo, ativo)
    data = get_data_v2(ticker, tf, velas_minimas=100)
    if data is None:
        detalhe = QUOTEX_OTC_FEED.diagnostico() if "-OTC" in ativo else "Verifique a fonte correspondente e as credenciais configuradas."
        return jsonify({"ok": False, "error": f"Não foi possível obter dados reais e fechados suficientes para o backtest. {detalhe}"}), 503
    return jsonify({"ok": True, "resultado": backtest_estrategia(data, estrategia, tf, modo_gale=modo_gale)})

@app.route('/resultado/<res>', methods=['POST'])
def resultado(res):
    user = session.get('user')
    if user:
        st = get_user_state(user)
        operacao = st.get("sinal_confirmado_dados") or {}

        if res in ("win", "g1", "red"):
            is_win = res in ("win", "g1")
            atualizar_estatisticas_usuario(user, is_win)
            resultado_bd = "Win" if res == "win" else ("WinG1" if res == "g1" else "Red")
            atualizar_ultimo_sinal_bd(user, resultado_bd)

            stats = obter_estatisticas_usuario(user)
            if res == "win":
                titulo = "🏆 WIN — OPERAÇÃO ENCERRADA COM RESULTADO POSITIVO"
                icone = "🟢"
                detalhe = "WIN DIRETO"
            elif res == "g1":
                titulo = "🔄 WIN G1 — OPERAÇÃO ENCERRADA COM RESULTADO POSITIVO"
                icone = "🟡"
                detalhe = "WIN G1"
            else:
                titulo = "🔴 RED — OPERAÇÃO ENCERRADA COM RESULTADO NEGATIVO"
                icone = "🔴"
                detalhe = "RED"

            msg_resultado = (
                f"{icone} <b>{titulo}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>RESULTADO:</b> {detalhe}\n"
                f"💱 <b>Paridade:</b> {operacao.get('ativo', 'N/D')}\n"
                f"↕️ <b>Direção:</b> {operacao.get('sinal', 'N/D')}\n"
                f"🧠 <b>Estratégia:</b> {operacao.get('estrategia_fmt', 'N/D')}\n"
                f"⏱ <b>Timeframe:</b> M{operacao.get('tf', st.get('timeframe', 5))}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>DESEMPENHO DA SESSÃO</b>\n"
                f"🟢 WIN: <b>{stats['wins']}</b>\n"
                f"🔴 RED: <b>{stats['reds']}</b>\n"
                f"📈 Assertividade: <b>{stats['winrate']:.1f}%</b>\n"
                f"📋 Total de operações: <b>{stats['total']}</b>\n\n"
                f"⚠️ <i>Resultado registrado no histórico. Operações envolvem risco e não há garantia de resultados futuros.</i>"
            )
            if user == ADMIN_EMAIL and telegram_envio_ativo():
                enviar_telegram(msg_resultado, user_solicitante=user)

        elif res == 'pular':
            # Ao PULAR, nenhuma mensagem relacionada à oportunidade deve permanecer
            # no canal: apagamos o alerta de preparação e também a mensagem enviada
            # quando o sinal foi confirmado.
            alerta_para_apagar = st.get("alerta_ativo") or {}
            ids_para_apagar = set()
            # Invalida qualquer confirmação em andamento antes de apagar os IDs.
            st["confirmacao_em_processamento"] = None

            for chave in ("msg_id", "msg_id_confirmacao", "msg_id_sinal_confirmado"):
                valor = alerta_para_apagar.get(chave)
                if valor:
                    ids_para_apagar.add(valor)

            dados_confirmados = st.get("sinal_confirmado_dados") or {}
            for chave in ("msg_id", "msg_id_confirmacao", "msg_id_sinal_confirmado"):
                valor = dados_confirmados.get(chave)
                if valor:
                    ids_para_apagar.add(valor)

            if st.get("timer_confirmacao"):
                try:
                    st["timer_confirmacao"].cancel()
                except Exception:
                    pass
            st["timer_confirmacao"] = None

            # Invalida primeiro o estado para impedir que uma thread assíncrona
            # publique novamente uma mensagem que acabou de ser cancelada.
            st["alerta_ativo"] = None
            st["sinal_confirmado_dados"] = None

            for msg_id in ids_para_apagar:
                try:
                    deletar_mensagem_telegram(msg_id)
                except Exception as e:
                    print(f"⚠️ Falha ao apagar mensagem do PULAR (ID {msg_id}): {e}")

            atualizar_ultimo_sinal_bd(user, "Ignorado")
            # Não enviamos mensagem de PULAR ao canal: o usuário solicitou que
            # tanto a confirmação quanto o aviso de PULAR sejam removidos.


        st["aguardando_confirmacao"] = False
        st["sinal_permanente"] = None
        st["sinal_confirmado_dados"] = None
        if st.get("timer_confirmacao"):
            try:
                st["timer_confirmacao"].cancel()
            except Exception:
                pass
        st["timer_confirmacao"] = None
        st["alerta_ativo"] = None

        st["ultimo_sinal"] = f"<div class='system-console'>🔍 ANALISANDO VELAS: <b>{st['ativo_atual']}</b> (M{st['timeframe']})<br><span style='color:#00f2fe;'>[RETOMANDO VARREDURA COMPLETA]</span></div><div class='tech-scanner'></div>"

    return redirect('/')

# ================= ENVIO TELEGRAM ASSÍNCRONO =================
def enviar_telegram_em_background(mensagem, user_email, alert_id=None, deletar_msg_id=None, st=None):
    """Executa operações do Telegram fora do loop de análise."""
    def worker():
        try:
            if deletar_msg_id:
                try:
                    deletar_mensagem_telegram(deletar_msg_id)
                except Exception as e:
                    print(f"⚠️ Falha ao deletar alerta antigo no Telegram: {e}")
            # Se o alerta já foi substituído ou confirmado enquanto o Telegram
            # estava processando, NÃO envia a mensagem antiga.
            if st is not None and alert_id is not None:
                atual = st.get("alerta_ativo")
                if not atual or atual.get("alert_id") != alert_id:
                    return

            novo_id = enviar_telegram(mensagem, auto_delete=None, user_solicitante=user_email)
            if st is not None and alert_id is not None and novo_id:
                atual = st.get("alerta_ativo")
                if atual and atual.get("alert_id") == alert_id:
                    atual["msg_id"] = novo_id
                else:
                    # A confirmação/substituição aconteceu durante o envio.
                    # Apaga imediatamente a mensagem que acabou de chegar.
                    deletar_mensagem_telegram(novo_id)
        except Exception as e:
            print(f"⚠️ Erro no envio Telegram em background: {e}")
    threading.Thread(target=worker, daemon=True).start()


# ================= CLASSIFICAÇÃO DO MOVIMENTO =================
def classificar_movimento(estrategia, estrategias_confluentes=None):
    """Classifica a natureza técnica do sinal para exibição ao cliente.
    É uma descrição da lógica que gerou o sinal, não uma garantia de resultado.
    """
    nomes = [str(estrategia or "").upper()]
    if estrategias_confluentes:
        nomes.extend(str(x).upper() for x in estrategias_confluentes)
    if "REVERSAO" in nomes or "REVERSÃO" in nomes:
        return "REVERSÃO", "🔄"
    if "RETRACAO" in nomes or "RETRAÇÃO" in nomes or "LOGICA_DO_PRECO" in nomes:
        return "RETRAÇÃO", "↩️"
    if "RSI_MACD_MA" in nomes or "MHI1" in nomes:
        return "CONTINUAÇÃO", "➡️"
    return "CONTINUAÇÃO", "➡️"


# ================= CONFIRMAÇÃO PRECISA DO SINAL =================
def confirmar_alerta_agendado(user_email, alert_id):
    """
    Confirma o alerta no horário exato programado, independentemente do tempo
    gasto pela varredura dos ativos. Isso evita que uma varredura lenta faça
    a confirmação aparecer somente depois da virada do candle.
    """
    try:
        st = get_user_state(user_email)
        alerta = st.get("alerta_ativo")

        # O alerta pode ter sido substituído/cancelado antes do timer disparar.
        if not alerta or alerta.get("alert_id") != alert_id:
            return

        # Impede que o Timer e o fallback do bot confirmem o mesmo alerta duas vezes.
        if alerta.get("confirmacao_em_processamento") or st.get("confirmacao_em_processamento") == alert_id:
            return
        alerta["confirmacao_em_processamento"] = True
        st["confirmacao_em_processamento"] = alert_id

        if not st.get("bot_iniciado") or st.get("bot_pausado"):
            alerta.pop("confirmacao_em_processamento", None)
            st["confirmacao_em_processamento"] = None
            return

        ativo = alerta["ativo"]
        sinal = alerta["sinal"]
        est_fmt = alerta["estrategia_fmt"]
        str_saida = alerta["str_saida"]
        prob = alerta["probabilidade"]
        tf = alerta["tf"]
        str_entrada = alerta["str_entrada"]
        msg_alerta_id = alerta.get("msg_id")
        tipo_movimento, icone_movimento = classificar_movimento(
            alerta.get("estrategia"), alerta.get("estrategias_confluentes")
        )

        # Assim que a confirmação aparecer, o alerta de preparação deixa de ser
        # necessário no canal e é removido.
        if msg_alerta_id:
            deletar_mensagem_telegram(msg_alerta_id)

        cor_direcao = "#10b981" if sinal == "CALL" else "#ef4444"

        # Atualiza a tela ANTES de qualquer operação de rede/banco.
        st["sinal_permanente"] = (
            f"<div class='status-box' style='border-color:#00f2fe; background:rgba(0,242,254,0.1);'>"
            f"<h3 style='color:#00f2fe; margin-bottom:8px;'>🎯 SINAL CONFIRMADO!</h3>"
            f"<b>ATIVO:</b> {ativo}<br>"
            f"<b>DIREÇÃO DE ENTRADA:</b> <span style='color:{cor_direcao}; font-size:18px;'>{sinal}</span><br>"
            f"<b>ESTRATÉGIA:</b> <span style='color:#38ef7d;'>{est_fmt} (score {prob}/100)</span><br>"
            f"<b>MOVIMENTO:</b> {icone_movimento} <span style='color:#00f2fe;'>{tipo_movimento}</span><br>"
            f"<b>TIMEFRAME:</b> M{tf} | <b>ENTRADA:</b> {str_entrada} | <b>EXPIRAÇÃO:</b> {str_saida}"
            f"</div>"
        )
        st["aguardando_confirmacao"] = True
        st["sinal_confirmado_dados"] = {
            "ativo": ativo,
            "sinal": sinal,
            "estrategia_fmt": est_fmt,
            "probabilidade": prob,
            "contexto_timeframe_superior": alerta.get("contexto_timeframe_superior", "N/D"),
            "fonte_dados": alerta.get("fonte_dados", nome_fonte_ativo(ativo)),
            "tf": tf,
            "str_entrada": str_entrada,
            "str_saida": str_saida,
            "tipo_movimento": tipo_movimento,
            "icone_movimento": icone_movimento,
            "msg_id_confirmacao": None,
            "msg_id_sinal_confirmado": None
        }
        st["alerta_ativo"] = None
        st["timer_confirmacao"] = None
        st["confirmacao_em_processamento"] = None

        st["notificacao"] = {
            "id": str(time.time_ns()),
            "titulo": f"🎯 ENTRADA: {ativo} — {sinal}",
            "corpo": f"Direção: {sinal} | M{tf} | {est_fmt} | Entrada: {str_entrada}"
        }

        msg_sinal = (
            f"🎯 <b>SINAL CONFIRMADO — ENTRADA AGORA!</b> 🎯\n\n"
            f"💱 <b>Paridade:</b> {ativo}\n"
            f"↕️ <b>DIREÇÃO DE ENTRADA:</b> {sinal}\n"
            f"⏱ <b>Timeframe:</b> M{tf}\n"
            f"🧠 <b>Estratégia:</b> {est_fmt}\n"
            f"🔥 <b>Score Técnico:</b> {prob}%\n"
            f"🕐 <b>Entrada:</b> {str_entrada}\n"
            f"⌛ <b>Expiração:</b> {str_saida}\n\n"
            f"💡 <i>Gerencie seu capital com responsabilidade.</i>"
        )

        def finalizar_confirmacao(
            _user=user_email, _ativo=ativo, _sinal=sinal,
            _est_fmt=est_fmt, _tf=tf, _msg=msg_sinal
        ):
            try:
                registrar_sinal_bd(
                    _user,
                    f"{_ativo} | {_sinal} | {_est_fmt} | M{_tf}",
                    ativo=_ativo,
                    direcao=_sinal,
                    timeframe=_tf,
                    estrategia=_est_fmt,
                    score=int(prob),
                    mercado=st.get("tipo_mercado", "TODOS"),
                    contexto_timeframe=(st.get("sinal_confirmado_dados") or {}).get("contexto_timeframe_superior", "N/D")
                )
            except Exception as e:
                print(f"⚠️ Erro ao registrar sinal confirmado: {e}")
            try:
                msg_confirmacao_id = enviar_telegram(
                    _msg, auto_delete=None, user_solicitante=_user
                )
                # Guarda o ID fora de alerta_ativo porque ele é zerado após a confirmação.
                dados = st.get("sinal_confirmado_dados") or {}
                if msg_confirmacao_id:
                    dados["msg_id_confirmacao"] = msg_confirmacao_id
                    dados["msg_id_sinal_confirmado"] = msg_confirmacao_id
                    st["sinal_confirmado_dados"] = dados
                    # Se o sinal foi pulado enquanto o envio estava em andamento,
                    # remove imediatamente a mensagem recém-chegada.
                    if not st.get("aguardando_confirmacao"):
                        deletar_mensagem_telegram(msg_confirmacao_id)
            except Exception as e:
                print(f"⚠️ Erro ao enviar confirmação Telegram: {e}")

        # Banco/Telegram ficam fora do caminho crítico da confirmação.
        threading.Thread(target=finalizar_confirmacao, daemon=True).start()

    except Exception as e:
        print(f"⚠️ Erro na confirmação agendada ({user_email}): {e}")


# ================= LOOP PRINCIPAL MULTI-USUÁRIO DO BOT =================
def bot_loop():
    ohlc_cache = {}
    ultimo_gc = time.time()

    while True:
        try:
            usuarios_ativos = list(DADOS_USUARIOS.items())
            
            if not usuarios_ativos:
                time.sleep(1)
                continue

            agora_scan = agora_brasilia()
            now_ts = time.time()

            # Cache curto e limitado: evita crescimento de RAM quando há muitos
            # usuários/ativos. Os dados são reais e podem ser buscados novamente.
            ohlc_cache = {k: v for k, v in ohlc_cache.items() if now_ts - v["time"] < 5}
            if len(ohlc_cache) > 120:
                excesso = len(ohlc_cache) - 120
                chaves_antigas = sorted(ohlc_cache, key=lambda k: ohlc_cache[k].get("time", 0))[:excesso]
                for chave_antiga in chaves_antigas:
                    ohlc_cache.pop(chave_antiga, None)

            for user_email, st in usuarios_ativos:
                try:
                    if not st.get("bot_iniciado") or st.get("bot_pausado"):
                        continue

                    if now_ts < st.get("inicio_varredura", 0):
                        continue

                    tf = st.get("timeframe", 5)
                    mkt = st.get("tipo_mercado", "TODOS")
                    user_est = st.get("estrategia", "TODAS")

                    # -------------------------------------------------------------
                    # 1. CONFIRMAÇÃO AGENDADA
                    # -------------------------------------------------------------
                    # A confirmação principal é disparada por threading.Timer no
                    # momento exato. Mantemos aqui apenas um fallback caso o timer
                    # seja atrasado pelo sistema.
                    alerta = st.get("alerta_ativo")
                    if alerta:
                        momento_confirmacao = alerta.get(
                            "momento_confirmacao",
                            alerta["prox_minuto_entrada"] - timedelta(seconds=5)
                        )
                        if agora_scan >= momento_confirmacao:
                            confirmar_alerta_agendado(
                                user_email, alerta.get("alert_id")
                            )
                            alerta = st.get("alerta_ativo")

                    bloquear_novos_alertas = st.get("aguardando_confirmacao", False)

                    # -------------------------------------------------------------
                    # 2. VARREDURA DINÂMICA DA SELEÇÃO DE ATIVOS
                    # -------------------------------------------------------------
                    ativos_por_mercado = {
                        "TODOS": ATIVOS_OPERAVEIS,
                        "ABERTO_TODOS": ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"],
                        "OTC_TODOS": ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"],
                        "FOREX_ABERTO": ATIVOS_BASE["FOREX_ABERTO"],
                        "FOREX_OTC": ATIVOS_BASE["FOREX_OTC"],
                        "CRIPTO_ABERTO": ATIVOS_BASE["CRIPTO_ABERTO"],
                        "CRIPTO_OTC": ATIVOS_BASE["CRIPTO_OTC"]
                    }
                    ativos_mercado = list(ativos_por_mercado.get(mkt, ATIVOS_OPERAVEIS))
                    selecao = normalizar_selecao_ativos(st.get("ativos_selecionados", ["TODOS"]))
                    if selecao == ["TODOS"]:
                        ativos_reais = ativos_mercado
                    else:
                        # A seleção explícita pode combinar Forex/Cripto e Aberto/OTC.
                        ativos_reais = [a for a in selecao if a in ativos_mercado] if mkt != "TODOS" else list(selecao)

                    if not ativos_reais:
                        st["ativo_atual"] = "NENHUM ATIVO DISPONÍVEL"
                        st["fonte_dados"] = "N/D"
                        continue

                    ativos_scan = ativos_reais.copy()
                    if selecao == ["TODOS"]:
                        random.shuffle(ativos_scan)

                    for ativo in ativos_scan:
                        if not st.get("bot_iniciado") or st.get("bot_pausado"):
                            break

                        st["ativo_atual"] = ativo
                        agora_candle = agora_brasilia()
                        segundos_desde_inicio = (agora_candle.minute % tf) * 60 + agora_candle.second
                        st["candle_decorrido"] = segundos_desde_inicio
                        st["candle_restante"] = max(0, (tf * 60) - segundos_desde_inicio)
                        ticker = ativo if "-OTC" in ativo.upper() else MAPA_TICKERS.get(ativo, ativo)
                        st["fonte_dados"] = nome_fonte_ativo(ativo)

                        if not alerta and not st.get("aguardando_confirmacao"):
                            st["ultimo_sinal"] = f"<div class='system-console'>🔍 VARRENDO 30 VELAS EM: <b style='color:#00f2fe; font-size:16px;'>{ativo}</b> (M{tf})</div><div class='tech-scanner'></div>"

                        cache_key = f"{ticker}_{tf}"
                        if cache_key in ohlc_cache:
                            data = ohlc_cache[cache_key]["data"]
                        else:
                            data = get_data_v2(ticker, tf, velas_minimas=100)
                            if data:
                                ohlc_cache[cache_key] = {"data": data, "time": time.time()}

                        if not data:
                            if "-OTC" in ativo.upper():
                                st["fonte_dados"] = "Quotex OTC — sem dados"
                                erro_qx = QUOTEX_OTC_FEED.diagnostico()
                                st["ultimo_sinal"] = (
                                    "<div class='system-console' style='color:#f59e0b;'>"
                                    f"⚠️ <b>QUOTEX OTC SEM DADOS</b><br>{erro_qx}"
                                    "</div>"
                                )
                            continue

                        if "-OTC" in ativo.upper():
                            st["fonte_dados"] = "Quotex OTC ✓"

                        sinal_encontrado = None
                        est_nome_encontrada = None
                        maior_prob = 0
                        confluencia_encontrada = 0
                        forca_encontrada = 0
                        estrategias_confluentes = []

                        if user_est == "TODAS":
                            estrategias_para_analisar = LISTA_ESTRATEGIAS.copy()
                        elif "," in str(user_est):
                            estrategias_para_analisar = [e.strip() for e in user_est.split(",") if e.strip() in LISTA_ESTRATEGIAS]
                        elif user_est in LISTA_ESTRATEGIAS:
                            estrategias_para_analisar = [user_est]
                        else:
                            estrategias_para_analisar = LISTA_ESTRATEGIAS.copy()

                        # Analisa todas as estratégias para identificar não apenas
                        # a maior probabilidade, mas também confluência de direção.
                        candidatos = []
                        for est_nome in estrategias_para_analisar:
                            sinal_test, prob_test = analisar_estrategia(data, est_nome)
                            if sinal_test and prob_test:
                                candidatos.append({
                                    "estrategia": est_nome,
                                    "sinal": sinal_test,
                                    "probabilidade": prob_test,
                                    "forca": forca_estrategia(est_nome)
                                })

                        if candidatos:
                            # Para cada direção, conta quantas estratégias concordam.
                            for candidato in candidatos:
                                candidato["confluencia"] = sum(
                                    1 for outro in candidatos
                                    if outro["sinal"] == candidato["sinal"]
                                )

                            # Probabilidade é o critério principal. Em empate,
                            # confluência e força da estratégia desempatarão.
                            escolhido = max(
                                candidatos,
                                key=lambda x: (
                                    x["probabilidade"],
                                    x["confluencia"],
                                    x["forca"]
                                )
                            )

                            sinal_encontrado = escolhido["sinal"]
                            est_nome_encontrada = escolhido["estrategia"]
                            maior_prob = escolhido["probabilidade"]
                            confluencia_encontrada = escolhido["confluencia"]
                            forca_encontrada = escolhido["forca"]
                            estrategias_confluentes = [
                                c["estrategia"] for c in candidatos
                                if c["sinal"] == sinal_encontrado
                            ]

                        price_action_qualificado = False
                        if "PRICE_ACTION" in estrategias_para_analisar:
                            pa_sinal, pa_prob, pa_conf = analisar_price_action(data)
                            price_action_qualificado = bool(pa_sinal and pa_conf >= 3 and pa_sinal == sinal_encontrado)

                        confluencia_real = (confluencia_encontrada >= 2 or price_action_qualificado)

                        contexto_ok = False
                        contexto_direcao = "SEM_CONTEXTO"
                        if sinal_encontrado and confluencia_real:
                            contexto_ok, contexto_direcao = validar_contexto_multitimeframe(
                                ticker, tf, sinal_encontrado, ohlc_cache
                            )

                        if sinal_encontrado and confluencia_real and contexto_ok and not bloquear_novos_alertas:
                            agora = agora_brasilia()
                            
                            min_pass = agora.minute % tf
                            seg_pass = min_pass * 60 + agora.second
                            total_seg = tf * 60
                            seg_restantes = total_seg - seg_pass

                            # A janela de decisão fecha 5 segundos antes da virada.
                            if seg_restantes <= 5:
                                continue

                            prox_minuto_entrada = agora + timedelta(seconds=seg_restantes)
                            momento_confirmacao = prox_minuto_entrada - timedelta(seconds=5)
                            horario_saida = prox_minuto_entrada + timedelta(minutes=tf)

                            # Horário em que o painel/Telegram confirmam a entrada.
                            str_entrada = momento_confirmacao.strftime("%H:%M:%S")
                            str_saida = horario_saida.strftime("%H:%M")

                            nome_est_formatado = NOME_ESTRATEGIAS_DISPLAY.get(est_nome_encontrada, est_nome_encontrada)

                            # O canal mantém SOMENTE um alerta de preparação por vez.
                            # REGRA DE SUBSTITUIÇÃO:
                            # 1) Probabilidade maior -> substitui.
                            # 2) Probabilidade menor -> NÃO substitui.
                            # 3) Probabilidade igual -> só substitui se houver
                            #    estratégia mais forte OU maior confluência.
                            # Assim, um ativo novo com a mesma porcentagem não toma
                            # o lugar do alerta atual sem um diferencial técnico.
                            if alerta:
                                ativo_anterior = alerta.get("ativo")
                                prob_anterior = alerta.get("probabilidade", 0)
                                confluencia_anterior = alerta.get("confluencia", 1)
                                forca_anterior = alerta.get("forca_estrategia", 0)

                                if maior_prob > prob_anterior:
                                    deve_substituir = True
                                    motivo_alerta = "MAIOR PROBABILIDADE DETECTADA"
                                elif maior_prob < prob_anterior:
                                    deve_substituir = False
                                    motivo_alerta = "PROBABILIDADE INFERIOR — ALERTA MANTIDO"
                                else:
                                    deve_substituir = (
                                        confluencia_encontrada > confluencia_anterior
                                        or forca_encontrada > forca_anterior
                                    )
                                    if confluencia_encontrada > confluencia_anterior and forca_encontrada > forca_anterior:
                                        motivo_alerta = "MESMA PROBABILIDADE + MAIOR CONFLUÊNCIA E FORÇA"
                                    elif confluencia_encontrada > confluencia_anterior:
                                        motivo_alerta = "MESMA PROBABILIDADE + MAIOR CONFLUÊNCIA"
                                    elif forca_encontrada > forca_anterior:
                                        motivo_alerta = "MESMA PROBABILIDADE + ESTRATÉGIA MAIS FORTE"
                                    else:
                                        motivo_alerta = "MESMA PROBABILIDADE — ALERTA MANTIDO"

                                # Se for o mesmo ativo e houver apenas a mesma
                                # qualidade, não cria um novo alerta desnecessariamente.
                                if ativo == ativo_anterior and maior_prob == prob_anterior and not deve_substituir:
                                    continue

                                if deve_substituir:
                                    msg_antigo_id = alerta.get("msg_id")
                                    novo_alert_id = str(time.time_ns())
                                    if ativo != ativo_anterior and maior_prob > prob_anterior:
                                        motivo_alerta = "NOVO ATIVO + MAIOR PROBABILIDADE"
                                    elif ativo != ativo_anterior and maior_prob == prob_anterior:
                                        motivo_alerta = motivo_alerta

                                    msg_pre_alerta = (
                                        f"⚡ <b>ALERTA ATUALIZADO — {motivo_alerta}</b> ⚡\n\n"
                                        f"<b>Ativo:</b> {ativo} ({maior_prob} pontos de score)\n"
                                        f"<b>Timeframe:</b> M{tf}\n"
                                        f"<b>DIREÇÃO DE ENTRADA:</b> {sinal_encontrado}\n"
                                        f"<b>Estratégia principal:</b> {nome_est_formatado}\n"
                                        f"<b>Tipo de movimento:</b> {classificar_movimento(est_nome_encontrada, estrategias_confluentes)[1]} {classificar_movimento(est_nome_encontrada, estrategias_confluentes)[0]}\n"
                                        f"<b>Horário da Entrada:</b> {str_entrada}\n\n"
                                        f"👉 <i>O alerta anterior foi cancelado. Considere somente este novo alerta.</i>"
                                    )

                                    # Troca o alerta no painel imediatamente.
                                    st["alerta_ativo"] = {
                                        "ativo": ativo,
                                        "sinal": sinal_encontrado,
                                        "estrategia": est_nome_encontrada,
                                        "estrategia_fmt": nome_est_formatado,
                                        "probabilidade": maior_prob,
                                        "confluencia": confluencia_encontrada,
                                        "forca_estrategia": forca_encontrada,
                                        "estrategias_confluentes": estrategias_confluentes,
                                        "contexto_timeframe_superior": contexto_direcao,
                                        "msg_id": None,
                                        "str_entrada": str_entrada,
                                        "str_saida": str_saida,
                                        "prox_minuto_entrada": prox_minuto_entrada,
                                        "momento_confirmacao": momento_confirmacao,
                                        "alert_id": novo_alert_id,
                                        "tf": tf,
                                        "tipo_movimento": classificar_movimento(est_nome_encontrada, estrategias_confluentes)[0],
                                        "icone_movimento": classificar_movimento(est_nome_encontrada, estrategias_confluentes)[1],
                                        "confirmacao_em_processamento": False
                                    }

                                    # Reagenda a confirmação para o novo alerta.
                                    if st.get("timer_confirmacao"):
                                        try:
                                            st["timer_confirmacao"].cancel()
                                        except Exception:
                                            pass
                                    agora_timer = agora_brasilia()
                                    atraso_confirmacao = max(
                                        0.0, (momento_confirmacao - agora_timer).total_seconds()
                                    )
                                    timer_confirmacao = threading.Timer(
                                        atraso_confirmacao,
                                        confirmar_alerta_agendado,
                                        args=(user_email, novo_alert_id)
                                    )
                                    timer_confirmacao.daemon = True
                                    st["timer_confirmacao"] = timer_confirmacao
                                    timer_confirmacao.start()

                                    # Pré-alertas não são enviados ao Telegram.

                                    st["ultimo_sinal"] = (
                                        f"<div style='text-align:center; color:#f59e0b; font-family: sans-serif;'>"
                                        f"⚡ <b>ALERTA SUBSTITUÍDO ({motivo_alerta})</b> ⚡<br>"
                                        f"<b>NOVO ATIVO: {ativo}</b> | <b>DIREÇÃO: <span style='color:{'#10b981' if sinal_encontrado=='CALL' else '#ef4444'}'>{sinal_encontrado}</span></b> | Entrada às <b>{str_entrada}</b> (M{tf})<br>"
                                        f"<span style='font-size:12px; color:#00f2fe;'>Estratégia: <b>{nome_est_formatado}</b></span>"
                                        f"</div>"
                                    )
                                    alerta = st["alerta_ativo"]

                            else:
                                if st["sinais_enviados"].get(ativo) == str_entrada:
                                    continue

                                st["sinais_enviados"][ativo] = str_entrada

                                msg_pre_alerta = (
                                    f"⚠️ <b>ATENÇÃO: ANALISANDO OPORTUNIDADE DE OPERAÇÃO</b> ⚠️\n\n"
                                    f"<b>Ativo:</b> {ativo}\n"
                                    f"<b>Timeframe:</b> M{tf}\n"
                                    f"<b>DIREÇÃO DE ENTRADA:</b> {sinal_encontrado}\n"
                                    f"<b>Estratégia Identificada:</b> {nome_est_formatado}\n"
                                    f"<b>Tipo de movimento:</b> {classificar_movimento(est_nome_encontrada, estrategias_confluentes)[1]} {classificar_movimento(est_nome_encontrada, estrategias_confluentes)[0]}\n"
                                    f"<b>Score Técnico:</b> {maior_prob}%\n"
                                    f"<b>Horário da Entrada:</b> {str_entrada}\n\n"
                                    f"👉 <i>Abra o ativo na corretora e prepare-se!</i>"
                                )
                                
                                novo_alert_id = str(time.time_ns())

                                st["alerta_ativo"] = {
                                    "ativo": ativo,
                                    "sinal": sinal_encontrado,
                                    "estrategia": est_nome_encontrada,
                                    "estrategia_fmt": nome_est_formatado,
                                    "probabilidade": maior_prob,
                                    "contexto_timeframe_superior": contexto_direcao,
                                    "msg_id": None,
                                    "str_entrada": str_entrada,
                                    "str_saida": str_saida,
                                    "prox_minuto_entrada": prox_minuto_entrada,
                                    "momento_confirmacao": momento_confirmacao,
                                    "alert_id": novo_alert_id,
                                    "tf": tf,
                                    "tipo_movimento": classificar_movimento(est_nome_encontrada, estrategias_confluentes)[0],
                                    "icone_movimento": classificar_movimento(est_nome_encontrada, estrategias_confluentes)[1],
                                    "confirmacao_em_processamento": False
                                }

                                # Agenda a confirmação independente da varredura.
                                agora_timer = agora_brasilia()
                                atraso_confirmacao = max(
                                    0.0, (momento_confirmacao - agora_timer).total_seconds()
                                )
                                timer_confirmacao = threading.Timer(
                                    atraso_confirmacao,
                                    confirmar_alerta_agendado,
                                    args=(user_email, novo_alert_id)
                                )
                                timer_confirmacao.daemon = True
                                st["timer_confirmacao"] = timer_confirmacao
                                timer_confirmacao.start()

                                # Nenhum pré-alerta é enviado ao Telegram. O canal
                                # só recebe o sinal confirmado, se o ADM habilitar.

                                st["ultimo_sinal"] = (
                                    f"<div style='text-align:center; color:#f59e0b; font-family: sans-serif;'>"
                                    f"⚠️ <b>PREPARE O ATIVO: {ativo} ({maior_prob}%)</b> ⚠️<br>"
                                    f"<span style='color:#fff;'>DIREÇÃO: <b style='color:{'#10b981' if sinal_encontrado=='CALL' else '#ef4444'}'>{sinal_encontrado}</b> | Entrada às <b>{str_entrada}</b> (M{tf})</span><br>"
                                    f"<span style='font-size:12px; color:#00f2fe;'>Estratégia: <b>{nome_est_formatado}</b></span>"
                                    f"</div>"
                                )

                                st["notificacao"] = {
                                    "id": str(time.time()),
                                    "titulo": f"⚠️ PREPARE-SE: {ativo}",
                                    "corpo": f"Direção: {sinal_encontrado} | Entrada às {str_entrada} (M{tf}) via {nome_est_formatado} ({maior_prob}%)."
                                }
                                alerta = st["alerta_ativo"]

                except Exception as e_usr:
                    print(f"Erro no loop do usuario {user_email}: {e_usr}")

            if time.time() - ultimo_gc >= 30:
                gc.collect()
                ultimo_gc = time.time()
            time.sleep(0.5)
        except Exception as err:
            print(f"Erro no loop global do bot: {err}")
            gc.collect()
            time.sleep(2)

# ================= THREAD BACKGROUND =================
thread_iniciada = False
lock_thread = threading.Lock()

@app.before_request
def start_background_loop():
    global thread_iniciada
    if not thread_iniciada:
        with lock_thread:
            if not thread_iniciada:
                threading.Thread(target=bot_loop, daemon=True).start()
                thread_iniciada = True

if __name__ == '__main__':
    start_background_loop()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
