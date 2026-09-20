import requests
import time
import math
import pytz
import threading
import json
import sys
import random
import os
import logging
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
TOKEN_TELEGRAM = os.getenv("TOKEN_TELEGRAM", "").strip()
CHAT_ID_TELEGRAM = os.getenv("CHAT_ID_TELEGRAM", "-1002979466366")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@vision.com").strip().lower()

DB_URL = os.getenv("DB_URL") or os.getenv("DATABASE_URL", "").strip()

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
            "sinal_confirmado_dados": None,
            "notificacao": None,
            "notificacao_ultima_hora": 0.0,
            "ultimo_resumo_sessao": None,
            "telegram_enabled": False,
            "proximo_sinal_permitido_em": 0.0,
            "ultimo_alerta_timestamp": 0.0,
            "ultimo_ativo_sinal": None,
            "ultimo_sinal_direcao": None
        }
    return DADOS_USUARIOS[email_clean]

def get_client_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

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
    """Envia Telegram somente para o ADM e quando o ADM ativou o envio."""
    solicitante = (user_solicitante or "").strip().lower()
    if solicitante != ADMIN_EMAIL:
        print("🔒 Telegram bloqueado: somente o ADM pode enviar mensagens.")
        return None
    estado_admin = DADOS_USUARIOS.get(ADMIN_EMAIL, {})
    if not estado_admin.get("telegram_enabled", False):
        print("🔕 Telegram desativado pelo ADM no painel.")
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
APP_SECRET = os.getenv("FLASK_SECRET", "chave_secreta_vision_pro_ultra_premium_v3_security")
app = Flask(__name__)
app.secret_key = APP_SECRET
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

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
                <span style="color:#f59e0b;">IPs Cadastrados (Máx 2): <b>{{ info.ips_Formatados }}</b></span>
            </div>
            <form action="/adm/editar" method="POST">
                <input type="hidden" name="email_original" value="{{ email }}">
                <b>E-mail:</b> <input type="text" name="novo_email" value="{{ email }}">
                <b>Nova Senha (deixe em branco para manter):</b> <input type="password" name="nova_senha" placeholder="Alterar senha...">
                <b>Expira em:</b> {{ info.criado_em }}<br><br>
                <button type="submit" class="btn-adm blue">SALVAR ALTERAÇÕES</button>
                <a href="/adm/renovar/{{ email }}" class="btn-adm green">RENOVAR +30 DIAS</a>
                <a href="/adm/liberar_ip/{{ email }}" class="btn-adm orange">LIBERAR DISPOSITIVOS / IPS</a>
                {% if email != admin %}
                <a href="/adm/excluir/{{ email }}" class="btn-adm red" onclick="return confirm('Excluir?')">EXCLUIR</a>
                {% endif %}
            </form>
        </div>
    </div>
    {% endfor %}
</body>
</html>
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

        .broker-flex { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 5px; scrollbar-width: none; }
        .broker-flex::-webkit-scrollbar { display: none; }
        .btn-broker { min-width: 100px; flex: 1; border: 1px solid #1e293b; background: #0f172a; color: #cbd5e1; padding: 10px; border-radius: 8px; font-weight: 700; font-size: 11px; cursor: pointer; transition: 0.3s; text-align: center; white-space: nowrap;}
        .btn-broker:hover { color: #fff; border-color: #00f2fe; background: #1e293b; }

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

        <button class="btn-notify" id="btn-enable-notify" onclick="solicitarPermissaoNotificacao()">🔔 ATIVAR NOTIFICAÇÕES NO CELULAR</button>
        {% if user == admin %}
        <div style="background:#0b1120;border:1px solid rgba(0,242,254,.25);border-radius:12px;padding:12px;margin-bottom:12px;">
            <div style="font-size:11px;font-weight:800;color:#64748b;text-transform:uppercase;margin-bottom:8px;">CONTROLE EXCLUSIVO DO ADM</div>
            <button id="btn-telegram-toggle" class="btn-test-tg" style="margin-bottom:8px;" onclick="sendCommand('toggle_telegram')">📡 TELEGRAM: {{ 'ATIVO' if telegram_enabled else 'DESATIVADO' }}</button>
            <button class="btn-test-tg" onclick="sendCommand('test_telegram')">🧪 TESTAR CONEXÃO TELEGRAM</button>
            <div style="font-size:10px;color:#94a3b8;text-align:center;">Somente o ADM pode ativar o envio. Usuários comuns recebem sinais apenas no painel.</div>
        </div>
        {% endif %}

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

        <div id="broker-view-container">
            <button class="btn-close-broker" onclick="closeBrokerView()">❌ FECHAR CORRETORA</button>
            <iframe id="brokerIframe" class="broker-iframe-inline" src=""></iframe>
        </div>

        <div id="ticker-live-status" style="background: rgba(0, 242, 254, 0.05); border: 1px solid rgba(0, 242, 254, 0.2); border-radius: 12px; padding: 10px; margin-bottom: 12px; text-align: center; font-size: 12px;">
            MERCADO SELECIONADO: <b id="mkt-badge" style="color: #00f2fe;">{{ modo }}</b> | 
            ANALISANDO AGORA: <b id="current-asset" style="color: #38ef7d;">AGUARDANDO...</b>
        </div>

        <div class="status-box" id="panel-text">Aguardando Comando...</div>

        <div id="result-area" class="result-grid" style="display:none;">
            <button class="btn-res btn-res-win" onclick="fetch('/resultado/win')">WIN</button>
            <button class="btn-res btn-res-g1" onclick="fetch('/resultado/g1')">G1</button>
            <button class="btn-res btn-res-red" onclick="fetch('/resultado/red')">RED</button>
            <button class="btn-res btn-res-skip" onclick="fetch('/resultado/pular')">PULAR</button>
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
                    <label>ESTRATÉGIA OPERACIONAL</label>
                    <div class="select-wrapper">
                        <select class="modern-select" onchange="sendCommand('set_est_' + this.value)">
                            <option value="TODAS" {% if estrat == 'TODAS' %}selected{% endif %}>💎 TODAS (Analisar Todas as Estratégias)</option>
                            <option value="LOGICA_DO_PRECO" {% if estrat == 'LOGICA_DO_PRECO' %}selected{% endif %}>Lógica do Preço</option>
                            <option value="RSI_MACD_MA" {% if estrat == 'RSI_MACD_MA' %}selected{% endif %}>RSI + Cruzamento MACD + MA</option>
                            <option value="MHI1" {% if estrat == 'MHI1' %}selected{% endif %}>MHI 1 (+ Filtro Tendência)</option>
                            <option value="REVERSAO" {% if estrat == 'REVERSAO' %}selected{% endif %}>Reversão de Bandas</option>
                        </select>
                    </div>
                </div>
            </div>

            <span class="section-label" style="margin-top: 5px;">Plataformas de Operação</span>
            <div class="broker-flex">
                <button class="btn-broker" onclick="openBroker('https://qxbroker.com/pt/')">🌐 Quotex</button>
                <button class="btn-broker" onclick="openBroker('https://iqoption.com')">📈 IQ Option</button>
                <button class="btn-broker" onclick="openBroker('https://binomo.com')">🟡 Binomo</button>
                <button class="btn-broker" onclick="openBroker('https://pocketoption.com')">🟦 Pocket Opt.</button>
            </div>

            {% if user == admin %}
            <button onclick="location.href='/admin_panel'" style="width:100%; margin-top:15px; padding:12px; background:rgba(0,242,254,0.1); border:1px solid #00f2fe; color:#00f2fe; font-weight:bold; border-radius:10px; cursor:pointer;">🛡️ ABRIR PAINEL ADMINISTRATIVO</button>
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

        function openBroker(url) {
            const brokerContainer = document.getElementById('broker-view-container');
            document.getElementById('brokerIframe').src = url;
            brokerContainer.style.display = 'flex';
        }

        function closeBrokerView() {
            document.getElementById('broker-view-container').style.display = 'none';
            document.getElementById('brokerIframe').src = '';
        }

        function toggleHistorico() {
            const box = document.getElementById('box-historico');
            if (box.style.display === 'block') {
                box.style.display = 'none';
            } else {
                box.style.display = 'block';
            }
        }

        function sendCommand(cmd) {
            fetch('/command/' + cmd).then(r => r.json()).then(data => {
                if(data.redirect) window.location.href = data.redirect;
            });
        }

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

                const tgBtn = document.getElementById('btn-telegram-toggle');
                if (tgBtn && data.is_admin) {
                    tgBtn.innerText = data.telegram_enabled ? '📡 TELEGRAM: ATIVO' : '📡 TELEGRAM: DESATIVADO';
                    tgBtn.style.borderColor = data.telegram_enabled ? '#10b981' : '#3b82f6';
                    tgBtn.style.color = data.telegram_enabled ? '#10b981' : '#3b82f6';
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
                setTimeout(atualizarPainel, 250);
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

            CREATE TABLE IF NOT EXISTS historico_sinais (
                id SERIAL PRIMARY KEY,
                user_email VARCHAR(255) NOT NULL,
                sinal VARCHAR(255) NOT NULL,
                resultado VARCHAR(50) NOT NULL
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
            INSERT INTO usuarios (email, senha, criado_em, wins, reds, winrate, ips_autorizados)
            VALUES (%s, %s, %s, 0, 0, 0.0, %s)
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

def registrar_sinal_bd(email, sinal_str):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO historico_sinais (user_email, sinal, resultado)
            VALUES (%s, %s, %s);
        """, (email.strip().lower(), sinal_str, "Analisando..."))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass

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
            cur.execute("UPDATE historico_sinais SET resultado = %s WHERE id = %s;", (resultado, ultimo_id))
            conn.commit()

        cur.close()
        conn.close()
    except Exception:
        pass

# ================= BOT CONFIGS & ESTRATÉGIAS =================
LISTA_ESTRATEGIAS = ["LOGICA_DO_PRECO", "RSI_MACD_MA", "MHI1", "REVERSAO"]

NOME_ESTRATEGIAS_DISPLAY = {
    "LOGICA_DO_PRECO": "Lógica do Preço",
    "RSI_MACD_MA": "RSI + Cruzamento MACD + MA",
    "MHI1": "MHI 1 (+ Filtro Tendência)",
    "REVERSAO": "Reversão de Bandas",
    "TODAS": "Análise Dinâmica Múltipla"
}

# Peso relativo usado SOMENTE para desempatar sinais com a mesma probabilidade.
# A porcentagem continua sendo o primeiro critério; em empate, confluência e
# força da estratégia ajudam a decidir se um novo ativo realmente merece
# substituir o alerta atual. Esses pesos são configuráveis.
FORCA_ESTRATEGIA = {
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

# ================= MOTOR DE ANÁLISE REAL DE 30 VELAS =================
def get_data_v2(ticker, tf, velas_minimas=80):
    """Obtém OHLC + volume; nunca inventa candles quando a fonte falha."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*'
        }
        base_ticker = ticker
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{base_ticker}?interval={tf}m&range=10d"
        res = requests.get(url, headers=headers, timeout=6.0)
        if res.status_code == 200:
            result = (res.json().get('chart', {}).get('result') or [None])[0]
            if result:
                ts = result.get('timestamp') or []
                q = (result.get('indicators', {}).get('quote') or [{}])[0]
                opens=np.array(q.get('open') or [],dtype=float); highs=np.array(q.get('high') or [],dtype=float)
                lows=np.array(q.get('low') or [],dtype=float); closes=np.array(q.get('close') or [],dtype=float)
                vols=np.array(q.get('volume') or [],dtype=float)
                n=min(len(ts),len(opens),len(highs),len(lows),len(closes))
                if n>=velas_minimas:
                    opens,highs,lows,closes=opens[-n:],highs[-n:],lows[-n:],closes[-n:]; ts=np.array(ts[-n:])
                    vols=vols[-n:] if len(vols)>=n else np.zeros(n)
                    valid=np.isfinite(opens)&np.isfinite(highs)&np.isfinite(lows)&np.isfinite(closes)
                    opens,highs,lows,closes,ts,vols=opens[valid],highs[valid],lows[valid],closes[valid],ts[valid],vols[valid]
                    if len(closes)>=velas_minimas:
                        proxy=not np.any(vols>0)
                        if proxy: vols=np.abs(closes-opens)
                        return {"time":ts,"open":opens,"high":highs,"low":lows,"close":closes,"volume":vols,"volume_proxy":proxy}
        if "-USD" in base_ticker or "USD" in ticker:
            sym=ticker.replace("USD","").replace("-OTC","").replace("-","")
            url_alt=f"https://min-api.cryptocompare.com/data/v2/histominute?fsym={sym}&tsym=USD&limit=2000&aggregate={tf}"
            alt=requests.get(url_alt,timeout=6.0).json()
            arr=alt.get('Data',{}).get('Data',[]) if alt.get('Response')=='Success' else []
            if len(arr)>=velas_minimas:
                opens=np.array([x.get('open',np.nan) for x in arr],float); closes=np.array([x.get('close',np.nan) for x in arr],float)
                highs=np.array([x.get('high',np.nan) for x in arr],float); lows=np.array([x.get('low',np.nan) for x in arr],float)
                vols=np.array([x.get('volumeto',0) or x.get('volumefrom',0) or 0 for x in arr],float); ts=np.array([x.get('time',0) for x in arr])
                valid=np.isfinite(opens)&np.isfinite(highs)&np.isfinite(lows)&np.isfinite(closes)
                opens,closes,highs,lows,vols,ts=opens[valid],closes[valid],highs[valid],lows[valid],vols[valid],ts[valid]
                if len(closes)>=velas_minimas:
                    proxy=not np.any(vols>0)
                    if proxy: vols=np.abs(closes-opens)
                    return {"time":ts,"open":opens,"high":highs,"low":lows,"close":closes,"volume":vols,"volume_proxy":proxy}
    except Exception as e:
        print(f"⚠️ Falha ao obter dados de {ticker}: {e}")
    return None

def calcular_ema(dados, periodo):
    dados=np.asarray(dados,dtype=float)
    if len(dados)<periodo:return np.array(dados,dtype=float)
    ema=np.zeros_like(dados,dtype=float); k=2/(periodo+1); ema[periodo-1]=np.mean(dados[:periodo])
    for j in range(periodo,len(dados)): ema[j]=(dados[j]-ema[j-1])*k+ema[j-1]
    return ema

def calcular_rsi(dados, periodo=14):
    c=np.asarray(dados,dtype=float)
    if len(c)<periodo+2:return np.full(len(c),50.0)
    d=np.diff(c,prepend=c[0]); g=np.maximum(d,0); loss=np.maximum(-d,0)
    ag=np.zeros(len(c)); al=np.zeros(len(c)); ag[periodo]=np.mean(g[1:periodo+1]); al[periodo]=np.mean(loss[1:periodo+1])
    for j in range(periodo+1,len(c)): ag[j]=(ag[j-1]*(periodo-1)+g[j])/periodo; al[j]=(al[j-1]*(periodo-1)+loss[j])/periodo
    rs=ag/np.maximum(al,1e-12); r=100-(100/(1+rs)); r[:periodo]=50; return r

def calcular_atr(data, periodo=14):
    h,l,c=data["high"],data["low"],data["close"]; prev=np.roll(c,1); prev[0]=c[0]
    tr=np.maximum(h-l,np.maximum(np.abs(h-prev),np.abs(l-prev))); atr=np.zeros(len(c))
    if len(c)<periodo+1:return tr
    atr[periodo-1]=np.mean(tr[:periodo])
    for j in range(periodo,len(c)): atr[j]=(atr[j-1]*(periodo-1)+tr[j])/periodo
    return atr

def volume_confluente(data,sinal,i=-2):
    v=np.asarray(data.get("volume",[]),float); c,o,h,l=data["close"],data["open"],data["high"],data["low"]
    idx=i if i>=0 else len(c)+i
    if len(v)<25 or idx<20:return False,0.0,0.0
    base=v[idx-20:idx]; base=base[np.isfinite(base)&(base>0)]; atual=float(v[idx]) if np.isfinite(v[idx]) else 0
    if len(base)<10 or atual<=0:return False,0.0,0.0
    ratio=atual/max(float(np.median(base)),1e-12); amp=max(h[idx]-l[idx],1e-12); body=abs(c[idx]-o[idx])/amp
    pinf=(min(o[idx],c[idx])-l[idx])/amp; psup=(h[idx]-max(o[idx],c[idx]))/amp
    direcional=(c[idx]>o[idx]) if sinal=="CALL" else (c[idx]<o[idx]); rejeicao=(pinf>=.30) if sinal=="CALL" else (psup>=.30)
    ok=ratio>=1.05 and (direcional or rejeicao)
    if body<.08 and not rejeicao:ok=False
    score=min(100,50+(ratio-1)*35+(15 if (direcional or rejeicao) else 0))
    return ok,ratio,score

def regime_mercado(data,i=-2):
    c=data["close"]; idx=i if i>=0 else len(c)+i; e20=calcular_ema(c,20); e50=calcular_ema(c,50); atr=calcular_atr(data); rsi=calcular_rsi(c)
    return {"ema20":e20[idx],"ema50":e50[idx],"atr":atr[idx],"rsi":rsi[idx]}

def analisar_estrategia(data,estrategia,i=-2):
    """Analisa candle fechado; volume/atividade é filtro obrigatório."""
    c,o,h,l=data["close"],data["open"],data["high"],data["low"]
    if len(c)<60 or len(data.get("volume",[]))<60:return None,0
    idx=i if i>=0 else len(c)+i
    if idx<55:return None,0
    rsi=calcular_rsi(c); e20=calcular_ema(c,20); e50=calcular_ema(c,50); e12=calcular_ema(c,12); e26=calcular_ema(c,26); macd=e12-e26; sig=calcular_ema(macd,9); atr=calcular_atr(data)
    amp=max(h[idx]-l[idx],1e-12); body=abs(c[idx]-o[idx])/amp; pinf=(min(o[idx],c[idx])-l[idx])/amp; psup=(h[idx]-max(o[idx],c[idx]))/amp
    sinal=None; score=0
    if estrategia=="LOGICA_DO_PRECO":
        fundo=pinf>=.42 and psup<=.22 and c[idx]>o[idx]; topo=psup>=.42 and pinf<=.22 and c[idx]<o[idx]
        excomp=psup>=.50 and body<=.35 and rsi[idx]>=62; exvend=pinf>=.50 and body<=.35 and rsi[idx]<=38
        if fundo or exvend:sinal="CALL"; score=76+min(12,pinf*20)+(5 if rsi[idx]<=45 else 0)
        elif topo or excomp:sinal="PUT"; score=76+min(12,psup*20)+(5 if rsi[idx]>=55 else 0)
    elif estrategia=="RSI_MACD_MA":
        bull=macd[idx]>sig[idx] and macd[idx-1]<=sig[idx-1]; bear=macd[idx]<sig[idx] and macd[idx-1]>=sig[idx-1]
        bull_recent=np.any(macd[max(0,idx-2):idx+1]>sig[max(0,idx-2):idx+1]); bear_recent=np.any(macd[max(0,idx-2):idx+1]<sig[max(0,idx-2):idx+1])
        if (bull or bull_recent) and rsi[idx]<=48 and c[idx]>=e20[idx]*.998 and e20[idx]>=e50[idx]*.999:sinal="CALL"; score=78+min(10,(48-rsi[idx])*.45)+(4 if bull else 2)
        elif (bear or bear_recent) and rsi[idx]>=52 and c[idx]<=e20[idx]*1.002 and e20[idx]<=e50[idx]*1.001:sinal="PUT"; score=78+min(10,(rsi[idx]-52)*.45)+(4 if bear else 2)
    elif estrategia=="MHI1":
        cores=["G" if c[j]>o[j] else "R" if c[j]<o[j] else "D" for j in range(idx-2,idx+1)]
        if "D" not in cores:
            qg,qr=cores.count("G"),cores.count("R"); alta=e20[idx]>e50[idx]; baixa=e20[idx]<e50[idx]
            if qg==3 and rsi[idx]>=65 and not alta:sinal,score="PUT",82
            elif qr==3 and rsi[idx]<=35 and not baixa:sinal,score="CALL",82
            elif qg==2 and qr==1 and c[idx]<=e20[idx] and rsi[idx]>=55:sinal,score="PUT",79
            elif qr==2 and qg==1 and c[idx]>=e20[idx] and rsi[idx]<=45:sinal,score="CALL",79
    elif estrategia in ["REVERSAO","RETRACAO"]:
        win=c[max(0,idx-19):idx+1]; ma=float(np.mean(win)); std=float(np.std(win))
        if std>0:
            z=(c[idx]-ma)/std
            if z<=-2 and pinf>=.25 and rsi[idx]<=42:sinal,score="CALL",80+min(10,abs(z)*2.5)
            elif z>=2 and psup>=.25 and rsi[idx]>=58:sinal,score="PUT",80+min(10,abs(z)*2.5)
    if not sinal:return None,0
    vok,vr,_=volume_confluente(data,sinal,idx)
    if not vok:return None,0
    atrmed=max(float(np.mean(atr[max(0,idx-20):idx])),1e-12)
    if atr[idx]>atrmed*2.2:return None,0
    score+=min(8,max(0,(vr-1)*12))
    return sinal,int(min(96,max(75,round(score))))

def analisar_mercado_profundo(data,estrategias,minimo_confluencia=2):
    cand=[]
    for est in estrategias:
        sig,score=analisar_estrategia(data,est,-2)
        if sig and score:cand.append({"estrategia":est,"sinal":sig,"probabilidade":score,"forca":forca_estrategia(est)})
    if not cand:return None
    grupos={}
    for x in cand:grupos.setdefault(x["sinal"],[]).append(x)
    grupo=max(grupos.values(),key=lambda g:(len(g),max(x["probabilidade"] for x in g),sum(x["forca"] for x in g)))
    if len(grupo)<minimo_confluencia:return None
    esc=max(grupo,key=lambda x:(x["probabilidade"],x["forca"])); reg=regime_mercado(data,-2); atr=max(reg["atr"],1e-12)
    if abs(data["close"][-2]-reg["ema20"])/atr>2.5:return None
    out=dict(esc); out["estrategias_confluentes"]=[x["estrategia"] for x in grupo]; out["confluencia"]=len(grupo); out["forca"]=sum(x["forca"] for x in grupo); out["volume_ratio"]=volume_confluente(data,esc["sinal"],-2)[1]; out["rsi"]=reg["rsi"]; return out

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

        if e == ADMIN_EMAIL:
            try:
                salvar_usuario(e, s, agora_brasilia().strftime("%Y-%m-%d"), ip_inicial=None)
            except Exception as err:
                return render_template_string(HTML_LOGIN, erro=f"Erro ao registrar ADM: {err}")

        usuarios = carregar_usuarios()
        if e not in usuarios:
            return render_template_string(HTML_LOGIN, erro=f"Usuário não cadastrado ({e}). Faça o cadastro.")

        user_db = usuarios[e]
        if not check_password_hash(user_db['senha'], s):
            return render_template_string(HTML_LOGIN, erro="Senha Incorreta.")

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

        session['user'] = e
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
            
        try:
            salvar_usuario(e, s, ip_inicial=ip_cliente)
            session['user'] = e
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

@app.route('/adm/renovar/<email>')
def adm_renovar(email):
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    renovar_usuario_db(email)
    return redirect('/admin_panel')

@app.route('/adm/liberar_ip/<email>')
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

@app.route('/adm/excluir/<email>')
def adm_excluir(email):
    if session.get('user') != ADMIN_EMAIL: return abort(403)
    excluir_usuario_db(email)
    return redirect('/admin_panel')

@app.route('/')
def index():
    if 'user' not in session: return redirect('/login')
    user = session['user']
    USUARIOS_ONLINE[user] = time.time()
    st = get_user_state(user)
    return render_template_string(HTML_INDEX, modo=st["tipo_mercado"], tf=st["timeframe"], estrat=st["estrategia"], user=user, admin=ADMIN_EMAIL, telegram_enabled=bool(st.get("telegram_enabled", False)))

@app.route('/status')
def status():
    user = session.get('user')
    if not user: return jsonify({})
    USUARIOS_ONLINE[user] = time.time()
    
    st = get_user_state(user)
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
        "rodando": st["bot_iniciado"] and not st["bot_pausado"],
        "notificacao": st["notificacao"],
        "telegram_enabled": bool(st.get("telegram_enabled", False)) if user == ADMIN_EMAIL else False,
        "is_admin": user == ADMIN_EMAIL
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

@app.route('/command/<cmd>')
def command(cmd):
    user = session.get('user')
    if not user:
        return jsonify({"ok": False})
    
    st = get_user_state(user)

    if cmd == "toggle_telegram":
        if user != ADMIN_EMAIL:
            return jsonify({"ok": False, "error": "Apenas o ADM pode controlar o Telegram."}), 403
        st["telegram_enabled"] = not bool(st.get("telegram_enabled", False))
        status_tg = "ATIVADO" if st["telegram_enabled"] else "DESATIVADO"
        st["ultimo_sinal"] = f"<div class='system-console' style='color:{'#10b981' if st['telegram_enabled'] else '#f59e0b'};'>📡 <b>ENVIO TELEGRAM {status_tg}</b><br>{'As mensagens poderão ser enviadas pelo ADM.' if st['telegram_enabled'] else 'Os sinais continuarão somente no painel.'}</div>"
        return jsonify({"ok": True, "telegram_enabled": st["telegram_enabled"]})
    
    if cmd == "test_telegram":
        if user != ADMIN_EMAIL or not st.get("telegram_enabled", False):
            return jsonify({"ok": False, "error": "Telegram disponível somente para o ADM e precisa estar ATIVADO."}), 403
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
        st["proximo_sinal_permitido_em"] = 0.0
        st["ultimo_alerta_timestamp"] = 0.0
        st["ultimo_ativo_sinal"] = None
        st["ultimo_sinal_direcao"] = None
        
        st["ativo_atual"] = "INICIANDO VARREDURA..."
        st["ultimo_sinal"] = f"<div class='system-console'>⚡ <b>INICIANDO MOTOR DE ANÁLISE DINÂMICA</b><br><span style='color:#00f2fe;'>[VARRENDO TODOS OS ATIVOS...]</span></div><div class='tech-scanner'></div>"
        
        msg_inicio_telegram = (
            f"🚀 <b>SISTEMA VISION PRO V3 INICIADO</b>\n\n"
            f"🟢 <b>Status:</b> Análise de 30 velas ativada\n"
            f"👤 <b>Usuário:</b> {user}\n"
            f"📊 <b>Timeframe:</b> M{st['timeframe']}\n"
            f"🌐 <b>Mercado:</b> {st['tipo_mercado']}\n"
            f"⚙️ <b>Estratégia:</b> {NOME_ESTRATEGIAS_DISPLAY.get(st['estrategia'], st['estrategia'])}\n\n"
            f"<i>Varrendo gráficos em tempo real...</i>"
        )
        enviar_telegram(msg_inicio_telegram, user_solicitante=user)
        return jsonify({"ok": True})

    elif cmd == "pause_bot":
        st["bot_pausado"] = not st["bot_pausado"]
        status_txt = "[PAUSADO] VARREDURA EM PAUSA..." if st["bot_pausado"] else f"🔍 ANALISANDO: {st['ativo_atual']} (M{st['timeframe']})"
        st["ultimo_sinal"] = f"<div class='system-console' style='color:#f59e0b;'>{status_txt}</div>" if st["bot_pausado"] else f"<div class='system-console'>🔍 ANALISANDO 30 VELAS: <b>{st['ativo_atual']}</b> (M{st['timeframe']})<br><span style='color:#00f2fe;'>[VARREDURA CONTINUA]</span></div><div class='tech-scanner'></div>"
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
        ids_stop=set()
        for chave in ("msg_id","msg_id_confirmacao","msg_id_sinal_confirmado"):
            if alerta_para_apagar.get(chave): ids_stop.add(alerta_para_apagar[chave])
        dados_stop=st.get("sinal_confirmado_dados") or {}
        for chave in ("msg_id","msg_id_confirmacao","msg_id_sinal_confirmado"):
            if dados_stop.get(chave): ids_stop.add(dados_stop[chave])
        for mid in ids_stop: deletar_mensagem_telegram(mid)
        st["alerta_ativo"] = None
        st["sinal_confirmado_dados"] = None
        st["proximo_sinal_permitido_em"] = 0.0
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
        enviar_telegram(msg_encerramento, user_solicitante=user)
        return jsonify({"ok": True, "estatisticas": stats})

    elif cmd.startswith("tf_"): 
        st["timeframe"] = int(cmd.split('_')[1])
    elif cmd.startswith("mkt_"): 
        st["tipo_mercado"] = cmd.split('_', 1)[1] 
    elif cmd.startswith("set_est_"): 
        st["estrategia"] = cmd.replace("set_est_", "")
    
    return jsonify({"ok": True})

@app.route('/resultado/<res>')
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

        st["proximo_sinal_permitido_em"] = time.time() + max(10,st.get("timeframe",5)*30)
        st["ultimo_ativo_sinal"] = operacao.get("ativo") if operacao else st.get("ultimo_ativo_sinal")
        st["ultimo_sinal_direcao"] = operacao.get("sinal") if operacao else st.get("ultimo_sinal_direcao")
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

        # REVALIDAÇÃO FINAL: nova leitura antes da entrada. Se a confluência
        # desaparecer, o bot cancela o alerta em vez de forçar a operação.
        dados_revalidacao = get_data_v2(MAPA_TICKERS.get(ativo,ativo),tf,velas_minimas=80)
        if not dados_revalidacao:
            alerta.pop("confirmacao_em_processamento",None); st["confirmacao_em_processamento"]=None; return
        ests_revalidacao=[e for e in (alerta.get("estrategias_confluentes") or [alerta.get("estrategia")]) if e]
        analise_final=analisar_mercado_profundo(dados_revalidacao,ests_revalidacao,max(1,min(2,len(ests_revalidacao))))
        if not analise_final or analise_final.get("sinal")!=sinal:
            if msg_alerta_id: deletar_mensagem_telegram(msg_alerta_id)
            st["alerta_ativo"]=None; st["aguardando_confirmacao"]=False; st["sinal_permanente"]=None; st["sinal_confirmado_dados"]=None; st["confirmacao_em_processamento"]=None; st["timer_confirmacao"]=None
            st["ultimo_sinal"]="<div class='system-console' style='color:#f59e0b;'>⛔ <b>ENTRADA CANCELADA NA REVALIDAÇÃO</b><br>A confluência perdeu força antes da entrada. Nenhum sinal foi confirmado.</div>"
            return
        prob=min(prob,analise_final.get("probabilidade",prob))
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
            f"<b>ESTRATÉGIA:</b> <span style='color:#38ef7d;'>{est_fmt} ({prob}%)</span><br>"
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
            f"🔥 <b>Confiança Técnica:</b> {prob}%\n"
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
                    f"{_ativo} | {_sinal} | {_est_fmt} | M{_tf}"
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

    while True:
        try:
            usuarios_ativos = list(DADOS_USUARIOS.items())
            
            if not usuarios_ativos:
                time.sleep(1)
                continue

            agora_scan = agora_brasilia()
            now_ts = time.time()

            # Limpeza do cache de dados OHLC a cada 5 segundos
            ohlc_cache = {k: v for k, v in ohlc_cache.items() if now_ts - v["time"] < 5}

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
                    # 2. VARREDURA DINÂMICA DE TODOS OS ATIVOS DO MERCADO
                    # -------------------------------------------------------------
                    if mkt == "TODOS":
                        ativos = ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"] + ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]
                    elif mkt == "ABERTO_TODOS":
                        ativos = ATIVOS_BASE["FOREX_ABERTO"] + ATIVOS_BASE["CRIPTO_ABERTO"]
                    elif mkt == "OTC_TODOS":
                        ativos = ATIVOS_BASE["FOREX_OTC"] + ATIVOS_BASE["CRIPTO_OTC"]
                    else:
                        ativos = ATIVOS_BASE.get(mkt, ATIVOS_BASE["FOREX_ABERTO"])

                    ativos_scan = ativos.copy()
                    random.shuffle(ativos_scan)

                    for ativo in ativos_scan:
                        if not st.get("bot_iniciado") or st.get("bot_pausado"):
                            break

                        st["ativo_atual"] = ativo
                        ticker = MAPA_TICKERS.get(ativo, ativo)

                        if not alerta and not st.get("aguardando_confirmacao"):
                            st["ultimo_sinal"] = f"<div class='system-console'>🔍 VARRENDO 30 VELAS EM: <b style='color:#00f2fe; font-size:16px;'>{ativo}</b> (M{tf})<br><span style='color:#00f2fe;'>[CONFLUÊNCIA + VOLUME + CONTEXTO + REVALIDAÇÃO]</span></div><div class='tech-scanner'></div>"

                        cache_key = f"{ticker}_{tf}"
                        if cache_key in ohlc_cache:
                            data = ohlc_cache[cache_key]["data"]
                        else:
                            data = get_data_v2(ticker, tf, velas_minimas=30)
                            if data:
                                ohlc_cache[cache_key] = {"data": data, "time": time.time()}

                        if not data:
                            continue

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

                        # ANÁLISE PROFUNDA: confluência + volume + contexto + volatilidade.
                        if user_est == "TODAS":
                            estrategias_para_analisar = LISTA_ESTRATEGIAS.copy(); minimo_confluencia = 2
                        elif "," in str(user_est):
                            estrategias_para_analisar = [e.strip() for e in user_est.split(",") if e.strip() in LISTA_ESTRATEGIAS]; minimo_confluencia = min(2,len(estrategias_para_analisar))
                        elif user_est in LISTA_ESTRATEGIAS:
                            estrategias_para_analisar = [user_est]; minimo_confluencia = 1
                        else:
                            estrategias_para_analisar = LISTA_ESTRATEGIAS.copy(); minimo_confluencia = 2
                        analise = analisar_mercado_profundo(data,estrategias_para_analisar,minimo_confluencia)
                        sinal_encontrado = analise["sinal"] if analise else None
                        est_nome_encontrada = analise["estrategia"] if analise else None
                        maior_prob = analise["probabilidade"] if analise else 0
                        confluencia_encontrada = analise["confluencia"] if analise else 0
                        forca_encontrada = analise["forca"] if analise else 0
                        estrategias_confluentes = analise["estrategias_confluentes"] if analise else []
                        if time.time() < st.get("proximo_sinal_permitido_em",0):
                            continue

                        if sinal_encontrado and not bloquear_novos_alertas:
                            if user_est == "TODAS" and confluencia_encontrada < 2:
                                continue
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

                                    confluencia_txt = (
                                        f"{confluencia_encontrada} estratégias em confluência"
                                        if confluencia_encontrada > 1
                                        else "1 estratégia identificada"
                                    )
                                    msg_pre_alerta = (
                                        f"⚡ <b>ALERTA ATUALIZADO — {motivo_alerta}</b> ⚡\n\n"
                                        f"<b>Ativo:</b> {ativo} ({maior_prob}% de Assertividade)\n"
                                        f"<b>Timeframe:</b> M{tf}\n"
                                        f"<b>DIREÇÃO DE ENTRADA:</b> {sinal_encontrado}\n"
                                        f"<b>Estratégia principal:</b> {nome_est_formatado}\n"
                                        f"<b>Confluência:</b> {confluencia_txt}\n"
                                        f"<b>Volume/atividade:</b> {analise.get('volume_ratio',0):.2f}x da mediana\n"
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
                                        "volume_ratio": analise.get("volume_ratio",0) if analise else 0,
                                        "rsi": analise.get("rsi",50) if analise else 50,
                                        "analise_profunda": True,
                                        "estrategias_confluentes": estrategias_confluentes,
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
                                    st["ultimo_alerta_timestamp"]=time.time(); st["ultimo_ativo_sinal"]=ativo; st["ultimo_sinal_direcao"]=sinal_encontrado
                                    st["proximo_sinal_permitido_em"]=horario_saida.timestamp()+max(15,tf*30)

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

                                    enviar_telegram_em_background(
                                        msg_pre_alerta,
                                        user_email,
                                        alert_id=novo_alert_id,
                                        deletar_msg_id=msg_antigo_id,
                                        st=st
                                    )

                                    st["ultimo_sinal"] = (
                                        f"<div style='text-align:center; color:#f59e0b; font-family: sans-serif;'>"
                                        f"⚡ <b>ALERTA SUBSTITUÍDO ({motivo_alerta})</b> ⚡<br>"
                                        f"<b>NOVO ATIVO: {ativo}</b> | <b>DIREÇÃO: <span style='color:{'#10b981' if sinal_encontrado=='CALL' else '#ef4444'}'>{sinal_encontrado}</span></b> | Entrada às <b>{str_entrada}</b> (M{tf})<br>"
                                        f"<span style='font-size:12px; color:#00f2fe;'>Estratégia: <b>{nome_est_formatado}</b> | Confluência: <b>{confluencia_encontrada}</b></span>"
                                        f"</div>"
                                    )
                                    alerta = st["alerta_ativo"]

                            else:
                                if st["sinais_enviados"].get(ativo) == str_entrada:
                                    continue

                                st["sinais_enviados"][ativo] = str_entrada

                                confluencia_txt = (
                                    f"{confluencia_encontrada} estratégias em confluência"
                                    if confluencia_encontrada > 1
                                    else "1 estratégia identificada"
                                )
                                msg_pre_alerta = (
                                    f"⚠️ <b>ATENÇÃO: ANALISANDO OPORTUNIDADE DE OPERAÇÃO</b> ⚠️\n\n"
                                    f"<b>Ativo:</b> {ativo}\n"
                                    f"<b>Timeframe:</b> M{tf}\n"
                                    f"<b>DIREÇÃO DE ENTRADA:</b> {sinal_encontrado}\n"
                                    f"<b>Estratégia Identificada:</b> {nome_est_formatado}\n"
                                    f"<b>Confluência:</b> {confluencia_txt}\n"
                                    f"<b>Tipo de movimento:</b> {classificar_movimento(est_nome_encontrada, estrategias_confluentes)[1]} {classificar_movimento(est_nome_encontrada, estrategias_confluentes)[0]}\n"
                                    f"<b>Confiança Técnica:</b> {maior_prob}%\n"
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
                                    "confluencia": confluencia_encontrada,
                                    "forca_estrategia": forca_encontrada,
                                    "estrategias_confluentes": estrategias_confluentes,
                                    "volume_ratio": analise.get("volume_ratio",0) if analise else 0,
                                    "rsi": analise.get("rsi",50) if analise else 50,
                                    "analise_profunda": True,
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
                                st["ultimo_alerta_timestamp"]=time.time(); st["ultimo_ativo_sinal"]=ativo; st["ultimo_sinal_direcao"]=sinal_encontrado
                                st["proximo_sinal_permitido_em"]=horario_saida.timestamp()+max(15,tf*30)

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

                                enviar_telegram_em_background(
                                    msg_pre_alerta,
                                    user_email,
                                    alert_id=novo_alert_id,
                                    st=st
                                )

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

            time.sleep(0.5)
        except Exception as err:
            print(f"Erro no loop global do bot: {err}")
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
