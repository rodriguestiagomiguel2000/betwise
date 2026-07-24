# app.py - Parte 1: Imports, Configuração e Modelos (CORRIGIDA)

import os
import base64
import time
import json
import csv 
import io
import re
import socket  # <-- ADICIONADO
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import logging
from logging.handlers import RotatingFileHandler
import sys
from functools import wraps
import traceback

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_from_directory,
    jsonify
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import requests

from parser import parse_betslip_from_gemini

print("DEBUG: app.py loaded from", __file__)

# ====== CONFIG & ENV ======

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Dar permissões 777 no Render
if os.environ.get('RENDER') or os.environ.get('RENDER_EXTERNAL_URL'):
    try:
        os.chmod(UPLOAD_FOLDER, 0o777)
        print(f"✅ Permissões definidas para {UPLOAD_FOLDER}")
    except Exception as e:
        print(f"⚠️ Não foi possível definir permissões: {e}")

INSTANCE_PATH = os.path.join(BASE_DIR, 'instance')
if not os.path.exists(INSTANCE_PATH):
    os.makedirs(INSTANCE_PATH)

# Carregar .env apenas se existir (local)
if os.path.exists(os.path.join(BASE_DIR, ".env")):
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    print("✅ .env carregado localmente")
else:
    print("ℹ️ .env não encontrado - usando variáveis de ambiente do sistema")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

GEMINI_PROXY_URL = os.environ.get("GEMINI_PROXY_URL")
print(f"🔍 GEMINI_PROXY_URL: {'✅' if GEMINI_PROXY_URL else '❌'}")

# ====== DEBUG - VERIFICAR VARIÁVEIS DE AMBIENTE ======
print("=" * 60)
print("🔍 DEBUG - VARIÁVEIS DE AMBIENTE")
print("=" * 60)
print(f"   GEMINI_API_KEY: {'✅ DEFINIDA' if GEMINI_API_KEY else '❌ NÃO DEFINIDA'}")
if GEMINI_API_KEY:
    print(f"   - Tamanho: {len(GEMINI_API_KEY)} caracteres")
    print(f"   - Primeiros 10: {GEMINI_API_KEY[:10]}...")
print(f"   UPLOAD_FOLDER: {UPLOAD_FOLDER}")
print(f"   - Existe: {os.path.exists(UPLOAD_FOLDER)}")
print("=" * 60)

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY not set in .env or environment variables")

# Gemini Flash endpoint - usar Gemini 3.1 Flash Lite (500 requests/dia)
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-in-production")

# ===== CONFIGURAÇÃO DA BASE DE DADOS =====
database_url = os.environ.get('DATABASE_URL')

if database_url:
    # Forçar IPv4
    try:
        # Extrair hostname da URL
        # Formato: postgresql://user:pass@hostname:port/db
        parts = database_url.split('@')
        if len(parts) == 2:
            host_part = parts[1].split(':')[0]
            # Resolver hostname para IPv4
            ipv4 = socket.gethostbyname(host_part)
            print(f"🔍 Resolvido {host_part} para IPv4: {ipv4}")
            # Substituir hostname pelo IPv4
            database_url = database_url.replace(host_part, ipv4)
            print("✅ Forçando IPv4 para conexão PostgreSQL")
    except Exception as e:
        print(f"⚠️ Não foi possível resolver IPv4: {e}")
    
    # Garantir que é postgresql:// (não postgres://)
    database_url = re.sub(r'^postgres://', 'postgresql://', database_url)
    
    # Adicionar parâmetros de conexão se não existirem
    if '?' not in database_url:
        database_url += '?sslmode=require&connect_timeout=30'
    
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    print("✅ Usando PostgreSQL (Supabase)")
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(INSTANCE_PATH, "bets.db")
    print("⚠️ Usando SQLite (local)")

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# ===== CONFIGURAÇÕES DA APP =====
app.config['APP_NAME'] = 'BETWISE'
app.config['APP_TAGLINE'] = 'analytics · betting'
app.config['APP_LOGO'] = '📊'
app.config['APP_FAVICON'] = '📊'
app.config['APP_COLOR'] = '#00d4aa'

db = SQLAlchemy(app)

# ===== LOGIN MANAGER =====
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'

# ===== CONTEXT PROCESSOR =====
@app.context_processor
def inject_app_config():
    """Disponibiliza variáveis da app em todos os templates"""
    from datetime import datetime
    
    # Obter bankrolls do utilizador atual (se autenticado)
    all_bankrolls = []
    active_bankroll = None
    
    if current_user.is_authenticated:
        all_bankrolls = Bankroll.query.filter_by(user_id=current_user.id).order_by(Bankroll.name.asc()).all()
        active_bankroll = get_active_bankroll()
    
    return {
        'APP_NAME': app.config['APP_NAME'],
        'APP_TAGLINE': app.config['APP_TAGLINE'],
        'APP_LOGO': app.config['APP_LOGO'],
        'APP_FAVICON': app.config['APP_FAVICON'],
        'APP_COLOR': app.config['APP_COLOR'],
        'all_bankrolls_global': all_bankrolls,
        'active_bankroll': active_bankroll,
        'now': datetime.utcnow()
    }

# ===== USER LOADER =====
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ===== FUNÇÃO PARA OBTER BANKROLL ATIVO =====
def get_active_bankroll():
    """Retorna o bankroll ativo do utilizador atual ou None"""
    if current_user.is_authenticated:
        # Tentar encontrar o ativo
        active = Bankroll.query.filter_by(user_id=current_user.id, is_active=True).first()
        if active:
            return active
        
        # Se não houver ativo, definir o primeiro como ativo
        first = Bankroll.query.filter_by(user_id=current_user.id).first()
        if first:
            first.is_active = True
            db.session.commit()
            return first
        
        return None
    return None

# ===== FUNÇÃO PARA OBTER PRÓXIMO ID =====
def get_next_bet_id():
    """Get the next available bet ID"""
    try:
        result = db.session.execute(db.text("SELECT MAX(id) FROM bet")).scalar()
        if result:
            return result + 1
        return 1
    except Exception:
        last_bet = Bet.query.order_by(Bet.id.desc()).first()
        if last_bet:
            return last_bet.id + 1
        return 1

# ====== LOGGING ======
def setup_logging(app):
    """Configura o sistema de logging da aplicação"""
    for handler in app.logger.handlers[:]:
        app.logger.removeHandler(handler)
    
    log_level = logging.DEBUG if app.debug else logging.INFO
    app.logger.setLevel(log_level)
    
    log_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    if not os.path.exists('logs'):
        os.makedirs('logs')
    
    file_handler = RotatingFileHandler(
        'logs/betwise.log',
        maxBytes=10485760,
        backupCount=10
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(log_format)
    app.logger.addHandler(file_handler)
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if app.debug else logging.INFO)
    console_handler.setFormatter(log_format)
    app.logger.addHandler(console_handler)
    
    app.logger.info('=' * 50)
    app.logger.info('🚀 BETWISE iniciada')
    app.logger.info(f'📁 Ambiente: {"Produção" if not app.debug else "Desenvolvimento"}')
    app.logger.info('=' * 50)
    
    return app.logger

logger = setup_logging(app)

# ===== DECORADOR PARA LOGGING =====
def log_action(action_type):
    """Decorador para logging de ações dos utilizadores"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            start_time = time.time()
            try:
                result = f(*args, **kwargs)
                execution_time = time.time() - start_time
                if current_user.is_authenticated:
                    user_info = f"User: {current_user.username} (ID: {current_user.id})"
                else:
                    user_info = "User: Anonymous"
                app.logger.info(
                    f"ACTION: {action_type} | {user_info} | "
                    f"IP: {request.remote_addr} | "
                    f"Duration: {execution_time:.3f}s"
                )
                return result
            except Exception as e:
                if current_user.is_authenticated:
                    user_info = f"User: {current_user.username} (ID: {current_user.id})"
                else:
                    user_info = "User: Anonymous"
                app.logger.error(
                    f"ERROR: {action_type} | {user_info} | "
                    f"IP: {request.remote_addr} | "
                    f"Error: {str(e)}\n{traceback.format_exc()}"
                )
                raise
        return decorated_function
    return decorator

# ===== MODELOS =====
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    is_admin = db.Column(db.Boolean, default=False)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Bankroll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True)
    currency = db.Column(db.String(8), default="EUR")
    starting_balance = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    transactions = db.relationship(
        "Transaction", backref="bankroll", cascade="all, delete-orphan"
    )
    bookmaker_balances = db.relationship(
        "BankrollBookmakerBalance", 
        back_populates="bankroll", 
        cascade="all, delete-orphan"
    )
    
class Bookmaker(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True)
    starting_balance = db.Column(db.Float, default=0.0)
    currency = db.Column(db.String(8), default="EUR")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    
    bookmaker_balances = db.relationship(
        "BankrollBookmakerBalance", 
        back_populates="bookmaker", 
        cascade="all, delete-orphan"
    )

class Bet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bookmaker = db.Column(db.String(64))
    sport = db.Column(db.String(64))
    market_type = db.Column(db.String(64))
    total_odds = db.Column(db.Float)
    stake = db.Column(db.Float)
    potential_return = db.Column(db.Float)
    currency = db.Column(db.String(8), default="EUR")
    status = db.Column(db.String(16), default="open")
    placed_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)
    image_path = db.Column(db.String(256))
    raw_json = db.Column(db.Text)
    is_freebet = db.Column(db.Boolean, default=False)
    is_live = db.Column(db.Boolean, default=False)

    cashed_out_amount = db.Column(db.Float)
    cashed_out_at = db.Column(db.DateTime)

    bookmaker_id = db.Column(db.Integer, db.ForeignKey("bookmaker.id"))
    bankroll_id = db.Column(db.Integer, db.ForeignKey("bankroll.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    
    bookmaker_obj = db.relationship("Bookmaker")
    bankroll = db.relationship("Bankroll")
    legs = db.relationship("BetLeg", backref="bet", cascade="all, delete-orphan")

class BetLeg(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bet_id = db.Column(db.Integer, db.ForeignKey("bet.id"), nullable=False)
    event = db.Column(db.String(256))
    team = db.Column(db.String(128))
    market = db.Column(db.String(128))
    odds_decimal = db.Column(db.Float)
    status = db.Column(db.String(16), default="pending")
    is_builder = db.Column(db.Boolean, default=False)

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bankroll_id = db.Column(db.Integer, db.ForeignKey("bankroll.id"), nullable=False)
    type = db.Column(db.String(16))
    amount = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)   
    bookmaker_id = db.Column(db.Integer, db.ForeignKey("bookmaker.id"))
    bet_id = db.Column(db.Integer, db.ForeignKey("bet.id"))
    
    bookmaker_obj = db.relationship("Bookmaker")

class BankrollBookmakerBalance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bankroll_id = db.Column(db.Integer, db.ForeignKey("bankroll.id"), nullable=False)
    bookmaker_id = db.Column(db.Integer, db.ForeignKey("bookmaker.id"), nullable=False)
    starting_balance = db.Column(db.Float, default=0.0)
    current_balance = db.Column(db.Float, default=0.0)
    
    # Usar back_populates em vez de backref para evitar conflitos
    bankroll = db.relationship("Bankroll", back_populates="bookmaker_balances")
    bookmaker = db.relationship("Bookmaker", back_populates="bookmaker_balances")
    
    __table_args__ = (
        db.UniqueConstraint('bankroll_id', 'bookmaker_id', name='unique_bankroll_bookmaker'),
    )

class Tip(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    bet_id = db.Column(db.Integer, db.ForeignKey("bet.id"), nullable=False)
    title = db.Column(db.String(200))
    description = db.Column(db.Text)
    is_public = db.Column(db.Boolean, default=True)
    is_featured = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    likes = db.Column(db.Integer, default=0)
    views = db.Column(db.Integer, default=0)
    
    user = db.relationship("User", backref="tips")
    bet = db.relationship("Bet", backref="tips")

class TipComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tip_id = db.Column(db.Integer, db.ForeignKey("tip.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    tip = db.relationship("Tip", backref="comments")
    user = db.relationship("User", backref="tip_comments")

class UserLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    action = db.Column(db.String(64), nullable=False)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship("User", backref="logs")
    
    @staticmethod
    def log(user_id, action, details=None, request=None):
        log = UserLog(
            user_id=user_id,
            action=action,
            details=details,
            ip_address=request.remote_addr if request else None,
            user_agent=request.headers.get('User-Agent') if request else None
        )
        db.session.add(log)
        db.session.commit()
        return log

class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    endpoint = db.Column(db.String(512), nullable=False)
    auth_key = db.Column(db.String(256))
    p256dh_key = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship("User", backref="push_subscriptions")
    
# app.py - Parte 2: Migração e Inicialização da Base de Dados

# ===== MIGRAR BASE DE DADOS =====
def migrate_database():
    """Migrate database schema to add new columns"""
    import sqlite3
    from datetime import datetime
    
    db_path = os.path.join(INSTANCE_PATH, "bets.db")
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Criar tabela user se não existir
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='user'
        """)
        if not cursor.fetchone():
            print("Creating table user...")
            cursor.execute("""
                CREATE TABLE user (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username VARCHAR(64) UNIQUE NOT NULL,
                    email VARCHAR(120) UNIQUE NOT NULL,
                    password_hash VARCHAR(256) NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    is_admin BOOLEAN DEFAULT 0
                )
            """)
            print("Table user created.")
        
        # Verificar colunas da tabela bookmaker
        cursor.execute("PRAGMA table_info(bookmaker)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'user_id' not in columns:
            try:
                cursor.execute("ALTER TABLE bookmaker ADD COLUMN user_id INTEGER REFERENCES user(id)")
                print("Added user_id to bookmaker")
            except sqlite3.OperationalError as e:
                print(f"Could not add user_id to bookmaker: {e}")
        
        # Verificar colunas da tabela bankroll
        cursor.execute("PRAGMA table_info(bankroll)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'user_id' not in columns:
            try:
                cursor.execute("ALTER TABLE bankroll ADD COLUMN user_id INTEGER REFERENCES user(id)")
                print("Added user_id to bankroll")
            except sqlite3.OperationalError as e:
                print(f"Could not add user_id to bankroll: {e}")
        
        # Verificar colunas da tabela bet
        cursor.execute("PRAGMA table_info(bet)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'user_id' not in columns:
            try:
                cursor.execute("ALTER TABLE bet ADD COLUMN user_id INTEGER REFERENCES user(id)")
                print("Added user_id to bet")
            except sqlite3.OperationalError as e:
                print(f"Could not add user_id to bet: {e}")
        
        conn.commit()
        conn.close()
        print("Database migration completed successfully!")
        
    except Exception as e:
        print(f"Error during migration: {e}")
        try:
            with app.app_context():
                db.create_all()
                print("Database migration completed via SQLAlchemy!")
        except Exception as e2:
            print(f"Error during SQLAlchemy migration: {e2}")

# ===== CRIAR TABELAS E ADMIN =====
with app.app_context():
    migrate_database()
    db.create_all()
    
    # Atualizar utilizadores existentes sem user_id
    try:
        # Se houver um admin, associar registos sem user_id
        admin = User.query.filter_by(username='admin').first()
        if admin:
            # Atualizar bankrolls sem user_id
            bankrolls = Bankroll.query.filter(Bankroll.user_id.is_(None)).all()
            for b in bankrolls:
                b.user_id = admin.id
            # Atualizar bookmakers sem user_id
            bookmakers = Bookmaker.query.filter(Bookmaker.user_id.is_(None)).all()
            for b in bookmakers:
                b.user_id = admin.id
            # Atualizar bets sem user_id
            bets = Bet.query.filter(Bet.user_id.is_(None)).all()
            for b in bets:
                b.user_id = admin.id
            db.session.commit()
            print(f"✅ Updated {len(bankrolls)} bankrolls, {len(bookmakers)} bookmakers, {len(bets)} bets with admin user_id")
    except Exception as e:
        print(f"Error updating user_ids: {e}")
        db.session.rollback()
    
    # Criar utilizador admin se não existir
    admin_user = User.query.filter_by(username='admin').first()
    if not admin_user:
        print("🔧 Creating admin user...")
        admin = User(
            username='admin',
            email='admin@betwise.com',
            password_hash=generate_password_hash('admin123'),
            is_admin=True,
            is_active=True
        )
        db.session.add(admin)
        db.session.commit()
        print("✅ Admin user created! (username: admin, password: admin123)")
    else:
        print("✅ Admin user already exists.")

    # Se houver apenas um bankroll, definir como ativo
    bankrolls = Bankroll.query.filter_by(user_id=admin_user.id).all() if admin_user else []
    if len(bankrolls) == 1:
        bankroll = bankrolls[0]
        if bankroll and not bankroll.is_active:
            bankroll.is_active = True
            db.session.commit()
            print(f"✅ Bankroll '{bankroll.name}' set as active.")
    
    # Atualizar all_bankrolls_global para o context processor
    all_bankrolls = Bankroll.query.all()
    print(f"✅ Total bankrolls: {len(all_bankrolls)}")
    
# app.py - Parte 3: Gemini Integration e Funções Auxiliares

# ===== GEMINI INTEGRATION =====
def build_gemini_prompt() -> str:
    return """
You are an assistant that reads sports betting slips from images and extracts structured data.

From the provided image of a betting slip, extract the following fields and return ONLY valid JSON:

- bookmaker: Name of the sportsbook (string or null).
- sport: Sport of the bet (string or null).
- market_type: Single, Multiple, Accumulator, Bet Builder, etc. (string or null).
- stake: Amount staked (decimal number or null).
- potential_return: Potential payout if the bet wins (decimal number or null).
- currency: Currency symbol or code (string or null, e.g. "EUR").
- status: "open", "settled", "won", "lost", or null if unknown.
- placed_at: Date/time the bet was placed. If only day and month are visible, use format "DD/MM" (e.g. "15/07"). If full date is visible, use ISO format "YYYY-MM-DD". If no date is visible, set to null.
- bet_id: Bet identifier on the slip, if shown (string or null).
- total_odds: Combined decimal odds shown on the slip (e.g. 3.14) or null.
- legs: Array of objects with:
  - event: Match or fixture name (e.g. "FF Jaro vs FC Inter") (string or null).
  - team: Team or selection name (string or null).
  - market: Market description (e.g. "Winner (incl. OT)", "FC Inter total corners") (string or null).
  - odds_decimal: Decimal odds for this selection (number or null, or null if not shown on the slip).

Rules:
- Infer fields only from the slip. If a field is not shown, set it to null.
- Use decimal odds (e.g. 1.47, 1.90) only when they are explicitly shown for that leg.
- For bet builder slips where only a total combined odd is shown, set legs[].odds_decimal = null and use the combined odd as total_odds.
- Use the actual currency shown on the slip.
- For "legs", include one entry per selection in the bet.
- For the date: if only day and month are visible, return as "DD/MM" (e.g. "15/07"). If full date is visible, return as "YYYY-MM-DD". If no date is visible, set to null.
- Return ONLY a single JSON object matching the schema above. No explanations, no extra keys, no text outside of JSON.
"""

def call_gemini_on_betslip(image_path: str, max_retries: int = 3, base_delay: float = 2.0) -> Dict[str, Any]:
    """Chama a API Gemini através do Cloudflare Worker (contorna restrições de localização)."""
    
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not configured.")
    
    if not GEMINI_PROXY_URL:
        raise RuntimeError("GEMINI_PROXY_URL not configured. Please add it to environment variables.")
    
    prompt = build_gemini_prompt()

    with open(image_path, "rb") as f:
        image_bytes = f.read()
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    models_to_try = [
        "gemini-3.1-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-flash-latest",
    ]
    
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2048,
        }
    }

    last_error = None

    for model in models_to_try:
        print(f"🔍 Tentando modelo via proxy: {model}")
        
        for attempt in range(max_retries):
            try:
                # Chamar o proxy Cloudflare
                headers = {
                    "Content-Type": "application/json",
                    "X-API-Key": GEMINI_API_KEY,
                    "X-Model": model,
                }
                
                resp = requests.post(GEMINI_PROXY_URL, headers=headers, json=payload, timeout=120)
                print(f"   Status code: {resp.status_code}")
                
                if resp.status_code == 200:
                    print(f"✅ Modelo {model} funcionou via proxy!")
                    data = resp.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    
                    cleaned = text.strip()
                    if cleaned.startswith("```"):
                        cleaned = cleaned.strip("`")
                        if cleaned.lower().startswith("json"):
                            cleaned = cleaned[4:]
                        cleaned = cleaned.strip()
                    
                    return json.loads(cleaned)
                    
                elif resp.status_code == 429:
                    print(f"⚠️ Limite excedido (429)")
                    time.sleep(base_delay * (attempt + 1) * 2)
                    continue
                    
                else:
                    print(f"⚠️ Erro: {resp.status_code} - {resp.text[:200]}")
                    last_error = resp
                    break
                    
            except requests.Timeout:
                print(f"⚠️ Timeout com modelo {model}")
                last_error = "Timeout"
                continue
                
            except Exception as e:
                print(f"⚠️ Erro: {e}")
                last_error = e
                break
    
    raise RuntimeError(f"Todos os modelos falharam. Último erro: {last_error}")

def parse_bet_in_background(bet_id: int, image_path: str):
    from app import app, db, Bet, BetLeg
    with app.app_context():
        bet = Bet.query.get(bet_id)
        if not bet:
            return
        try:
            gemini_data = call_gemini_on_betslip(image_path)
            parsed = parse_betslip_from_gemini(gemini_data)
            bet.bookmaker = parsed.get("bookmaker")
            bet.sport = parsed.get("sport")
            bet.market_type = parsed.get("market_type")
            bet.total_odds = parsed.get("total_odds")
            bet.stake = parsed.get("stake")
            bet.potential_return = parsed.get("potential_return")
            bet.currency = parsed.get("currency") or bet.currency
            bet.status = parsed.get("status") or bet.status
            bet.raw_json = str(gemini_data)
            bet.placed_at = parsed.get("placed_at") or bet.placed_at
            bet.notes = f"Bet ID: {parsed.get('bet_id')}" if parsed.get("bet_id") else bet.notes
            BetLeg.query.filter_by(bet_id=bet.id).delete()
            for leg_data in parsed.get("legs") or []:
                leg = BetLeg(
                    bet_id=bet.id,
                    event=leg_data.get("event"),
                    team=leg_data.get("team"),
                    market=leg_data.get("market"),
                    odds_decimal=leg_data.get("odds_decimal"),
                )
                db.session.add(leg)
            db.session.commit()
        except Exception as e:
            bet.notes = f"AI parsing failed: {e}"
            db.session.commit()

# ===== FUNÇÃO DE PROFIT =====
def bet_profit(b: Bet) -> float:
    if b.is_freebet:
        if b.status == "won" and b.potential_return:
            return b.potential_return
        elif b.status == "cashed_out" and b.cashed_out_amount:
            return b.cashed_out_amount
        else:
            return 0.0
    if b.status == "won" and b.potential_return and b.stake:
        return b.potential_return - b.stake
    elif b.status == "lost" and b.stake:
        return -b.stake
    elif b.status == "cashed_out" and b.cashed_out_amount and b.stake:
        return b.cashed_out_amount - b.stake
    else:
        return 0.0

def get_odds_range(odds):
    if odds < 1.5:
        return '1.0-1.5'
    elif odds < 2.0:
        return '1.5-2.0'
    elif odds < 2.5:
        return '2.0-2.5'
    elif odds < 3.0:
        return '2.5-3.0'
    elif odds < 4.0:
        return '3.0-4.0'
    else:
        return '4.0+'
    
# app.py - Parte 4: Rotas de Autenticação

# ===== ROTAS DE AUTENTICAÇÃO =====

@app.route("/")
@login_required
def index():
    bets = Bet.query.filter_by(user_id=current_user.id).order_by(Bet.placed_at.desc()).all()
    return render_template("bets.html", bets=bets)

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash("Please fill in all fields", "error")
            return render_template("login.html")
        user = User.query.filter_by(username=username).first()
        if not user:
            flash("Invalid username or password", "error")
            return render_template("login.html")
        if not user.is_active:
            flash("Your account has been disabled. Please contact support.", "error")
            return render_template("login.html")
        if not user.check_password(password):
            flash("Invalid username or password", "error")
            return render_template("login.html")
        login_user(user, remember=True)
        UserLog.log(
            user_id=user.id,
            action="login",
            details=f"User logged in from {request.remote_addr}",
            request=request
        )
        next_url = request.args.get('next')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        flash(f"Welcome back, {user.username}! 👋", "success")
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/logout")
@login_required
@log_action("logout")
def logout():
    username = current_user.username
    UserLog.log(
        user_id=current_user.id,
        action="logout",
        details=f"User logged out",
        request=request
    )
    app.logger.info(f"🚪 User logged out: {username}")
    logout_user()
    flash("Logged out successfully", "success")
    return redirect(url_for("index"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not username or not email or not password:
            flash("All fields are required", "error")
            return render_template("register.html")
        if len(username) < 3 or len(username) > 20:
            flash("Username must be between 3 and 20 characters", "error")
            return render_template("register.html")
        if '@' not in email or '.' not in email:
            flash("Please enter a valid email address", "error")
            return render_template("register.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters", "error")
            return render_template("register.html")
        if password != confirm_password:
            flash("Passwords do not match", "error")
            return render_template("register.html")
        if User.query.filter_by(username=username).first():
            flash("Username already taken", "error")
            return render_template("register.html")
        if User.query.filter_by(email=email).first():
            flash("Email already registered", "error")
            return render_template("register.html")
        user = User(username=username, email=email, is_active=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        UserLog.log(
            user_id=user.id,
            action="register",
            details=f"New user registered from {request.remote_addr}",
            request=request
        )
        app.logger.info(f"📝 New user registered: {username} (ID: {user.id}) | IP: {request.remote_addr}")
        flash("Registration successful! 🎉 Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/profile")
@login_required
def profile():
    bets = Bet.query.filter_by(user_id=current_user.id).all()
    total_bets = len(bets)
    total_staked = sum(b.stake or 0 for b in bets)
    total_profit = 0
    won_bets = 0
    lost_bets = 0
    for b in bets:
        if b.is_freebet:
            if b.status == "won" and b.potential_return:
                total_profit += b.potential_return
                won_bets += 1
            elif b.status == "lost":
                lost_bets += 1
        else:
            if b.status == "won" and b.potential_return and b.stake:
                total_profit += b.potential_return - b.stake
                won_bets += 1
            elif b.status == "lost" and b.stake:
                total_profit -= b.stake
                lost_bets += 1
    resolved = won_bets + lost_bets
    win_rate = (won_bets / resolved * 100) if resolved > 0 else 0
    return render_template(
        "profile.html",
        user=current_user,
        total_bets=total_bets,
        total_staked=round(total_staked, 2),
        total_profit=round(total_profit, 2),
        win_rate=round(win_rate, 1)
    )

@app.route("/profile/update", methods=["POST"])
@login_required
def update_profile():
    username = request.form.get("username")
    email = request.form.get("email")
    if username and username != current_user.username:
        if User.query.filter_by(username=username).first():
            flash("Username already taken", "error")
            return redirect(url_for("profile"))
        current_user.username = username
    if email and email != current_user.email:
        if User.query.filter_by(email=email).first():
            flash("Email already registered", "error")
            return redirect(url_for("profile"))
        current_user.email = email
    db.session.commit()
    flash("Profile updated successfully!", "success")
    return redirect(url_for("profile"))

# app.py - Parte 5: Rota bets_list

@app.route("/bets")
@login_required
def bets_list():
    bankroll_id = request.args.get('bankroll_id', '')
    if not bankroll_id:
        active = get_active_bankroll()
        if active:
            bankroll_id = str(active.id)
    query = Bet.query.filter_by(user_id=current_user.id)
    if bankroll_id and bankroll_id.isdigit():
        query = query.filter(Bet.bankroll_id == int(bankroll_id))
    bets = query.order_by(Bet.placed_at.desc(), Bet.id.desc()).all()
    all_bankrolls = Bankroll.query.filter_by(user_id=current_user.id).order_by(Bankroll.name.asc()).all()
    bets_by_bankroll = {}
    for bet in bets:
        key = bet.bankroll.name if bet.bankroll else "Sem Banca"
        if key not in bets_by_bankroll:
            bets_by_bankroll[key] = []
        bets_by_bankroll[key].append(bet)
    return render_template(
        "bets.html", 
        bets=bets,
        bets_by_bankroll=bets_by_bankroll,
        all_bankrolls=all_bankrolls,
        selected_bankroll_id=bankroll_id,
    )

@app.route("/bets/<int:bet_id>/delete", methods=["POST"])
@login_required
def delete_bet(bet_id):
    bet = Bet.query.filter_by(id=bet_id, user_id=current_user.id).first_or_404()
    db.session.delete(bet)
    db.session.commit()
    flash(f"Bet #{bet_id} deleted successfully.", "success")
    return redirect(url_for("index"))

@app.route("/bets/delete_bulk", methods=["POST"])
@login_required
def delete_bets_bulk():
    try:
        data = request.get_json()
        bet_ids = data.get('bet_ids', [])
        if not bet_ids:
            return jsonify({'success': False, 'error': 'No bet IDs provided'}), 400
        deleted_count = 0
        errors = []
        for bet_id in bet_ids:
            try:
                bet = Bet.query.filter_by(id=bet_id, user_id=current_user.id).first()
                if bet:
                    db.session.delete(bet)
                    deleted_count += 1
                else:
                    errors.append(f"Bet #{bet_id} not found")
            except Exception as e:
                errors.append(f"Error deleting bet #{bet_id}: {str(e)}")
        db.session.commit()
        return jsonify({
            'success': True,
            'deleted': deleted_count,
            'errors': errors,
            'message': f"Deleted {deleted_count} bets"
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route("/bets/<int:bet_id>/quick_update", methods=["POST"])
@login_required
def quick_update_bet(bet_id):
    bet = Bet.query.filter_by(id=bet_id, user_id=current_user.id).first_or_404()
    new_bet_status = request.form.get("bet_status")
    if new_bet_status:
        bet.status = new_bet_status
    for leg in bet.legs:
        field_name = f"leg_status_{leg.id}"
        new_leg_status = request.form.get(field_name)
        if new_leg_status:
            leg.status = new_leg_status
    has_lost = any(leg.status == "lost" for leg in bet.legs)
    all_won = bet.legs and all(leg.status == "won" for leg in bet.legs)
    if has_lost:
        bet.status = "lost"
    elif all_won:
        bet.status = "won"
    else:
        if not new_bet_status:
            bet.status = "open"
    # Sync transactions
    Transaction.query.filter_by(bet_id=bet.id).delete()
    if bet.is_freebet:
        if bet.status == "won" and bet.potential_return and bet.bankroll_id and bet.bookmaker_id:
            db.session.add(Transaction(
                bankroll_id=bet.bankroll_id,
                bookmaker_id=bet.bookmaker_id,
                bet_id=bet.id,
                type="deposit",
                amount=bet.potential_return,
                notes=f"Freebet payout for bet #{bet.id}"
            ))
        elif bet.status == "cashed_out" and bet.cashed_out_amount and bet.bankroll_id and bet.bookmaker_id:
            db.session.add(Transaction(
                bankroll_id=bet.bankroll_id,
                bookmaker_id=bet.bookmaker_id,
                bet_id=bet.id,
                type="deposit",
                amount=bet.cashed_out_amount,
                notes=f"Freebet cashout for bet #{bet.id}"
            ))
        db.session.commit()
        flash(f"Bet #{bet.id} updated from list.", "success")
        return redirect(url_for("index"))
    if bet.bankroll_id and bet.bookmaker_id and bet.stake and bet.status != "open":
        if bet.status in ("won", "lost", "cashed_out"):
            db.session.add(Transaction(
                bankroll_id=bet.bankroll_id,
                bookmaker_id=bet.bookmaker_id,
                bet_id=bet.id,
                type="withdrawal",
                amount=bet.stake,
                notes=f"Stake for bet #{bet.id}"
            ))
        if bet.status == "won" and bet.potential_return:
            db.session.add(Transaction(
                bankroll_id=bet.bankroll_id,
                bookmaker_id=bet.bookmaker_id,
                bet_id=bet.id,
                type="deposit",
                amount=bet.potential_return,
                notes=f"Payout for bet #{bet.id}"
            ))
        elif bet.status == "cashed_out" and bet.cashed_out_amount:
            db.session.add(Transaction(
                bankroll_id=bet.bankroll_id,
                bookmaker_id=bet.bookmaker_id,
                bet_id=bet.id,
                type="deposit",
                amount=bet.cashed_out_amount,
                notes=f"Cashout for bet #{bet.id}"
            ))
    db.session.commit()
    flash(f"Bet #{bet.id} updated from list.", "success")
    return redirect(url_for("index"))

@app.route("/bets/<int:bet_id>/add_leg", methods=["POST"])
@login_required
def add_leg(bet_id):
    bet = Bet.query.filter_by(id=bet_id, user_id=current_user.id).first_or_404()
    event = request.form.get("event") or None
    team = request.form.get("team") or None
    market = request.form.get("market") or None
    odds_raw = request.form.get("odds_decimal")
    odds_decimal = None
    if odds_raw:
        try:
            odds_decimal = float(odds_raw.replace(",", "."))
        except ValueError:
            odds_decimal = None
    is_builder = request.form.get("is_builder") == "1"
    leg = BetLeg(
        bet_id=bet.id,
        event=event,
        team=team,
        market=market,
        odds_decimal=odds_decimal,
        is_builder=is_builder,
    )
    db.session.add(leg)
    db.session.commit()
    flash("Leg added.", "success")
    return redirect(url_for("edit_bet", bet_id=bet.id))

@app.route("/bets/<int:bet_id>", methods=["GET", "POST"])
@login_required
def edit_bet(bet_id):
    bet = Bet.query.filter_by(id=bet_id, user_id=current_user.id).first_or_404()
    rolls = Bankroll.query.filter_by(user_id=current_user.id).order_by(Bankroll.name.asc()).all()
    books = Bookmaker.query.filter_by(user_id=current_user.id).order_by(Bookmaker.name.asc()).all()

    def parse_float_field(name: str) -> Optional[float]:
        val = request.form.get(name)
        if not val:
            return None
        try:
            return float(val.replace(",", "."))
        except ValueError:
            return None

    def sync_bet_transactions(bet_obj: Bet):
        Transaction.query.filter_by(bet_id=bet_obj.id).delete()
        if not bet_obj.bankroll_id or not bet_obj.bookmaker_id or not bet_obj.stake:
            return
        if bet_obj.status == "open":
            return
        if bet_obj.is_freebet:
            if bet_obj.status == "won" and bet_obj.potential_return:
                tx_payout = Transaction(
                    bankroll_id=bet_obj.bankroll_id,
                    bookmaker_id=bet_obj.bookmaker_id,
                    bet_id=bet_obj.id,
                    type="deposit",
                    amount=bet_obj.potential_return,
                    notes=f"Freebet payout for bet #{bet_obj.id}",
                )
                db.session.add(tx_payout)
            elif bet_obj.status == "cashed_out" and bet_obj.cashed_out_amount:
                tx_cashout = Transaction(
                    bankroll_id=bet_obj.bankroll_id,
                    bookmaker_id=bet_obj.bookmaker_id,
                    bet_id=bet_obj.id,
                    type="deposit",
                    amount=bet_obj.cashed_out_amount,
                    notes=f"Freebet cashout for bet #{bet_obj.id}",
                )
                db.session.add(tx_cashout)
            return
        if bet_obj.status in ("won", "lost", "cashed_out"):
            tx_stake = Transaction(
                bankroll_id=bet_obj.bankroll_id,
                bookmaker_id=bet_obj.bookmaker_id,
                bet_id=bet_obj.id,
                type="withdrawal",
                amount=bet_obj.stake,
                notes=f"Stake for bet #{bet_obj.id}",
            )
            db.session.add(tx_stake)
        if bet_obj.status == "won" and bet_obj.potential_return:
            tx_payout = Transaction(
                bankroll_id=bet_obj.bankroll_id,
                bookmaker_id=bet_obj.bookmaker_id,
                bet_id=bet_obj.id,
                type="deposit",
                amount=bet_obj.potential_return,
                notes=f"Payout for bet #{bet_obj.id}",
            )
            db.session.add(tx_payout)
        elif bet_obj.status == "cashed_out" and bet_obj.cashed_out_amount:
            tx_cashout = Transaction(
                bankroll_id=bet_obj.bankroll_id,
                bookmaker_id=bet_obj.bookmaker_id,
                bet_id=bet_obj.id,
                type="deposit",
                amount=bet_obj.cashed_out_amount,
                notes=f"Cashout for bet #{bet_obj.id}",
            )
            db.session.add(tx_cashout)

    if request.method == "POST":
        old_status = bet.status
        form_status = request.form.get("status")
        bankroll_id = request.form.get("bankroll_id")
        bookmaker_id = request.form.get("bookmaker_id")
        bet.bankroll_id = int(bankroll_id) if bankroll_id else None
        bet.bookmaker_id = int(bookmaker_id) if bookmaker_id else None
        bet.sport = request.form.get("sport") or bet.sport
        bet.market_type = request.form.get("market_type") or bet.market_type
        new_total_odds = parse_float_field("total_odds")
        if new_total_odds is not None:
            bet.total_odds = new_total_odds
        new_stake = parse_float_field("stake")
        if new_stake is not None:
            bet.stake = new_stake
        new_potential_return = parse_float_field("potential_return")
        if new_potential_return is not None:
            bet.potential_return = new_potential_return
        bet.currency = request.form.get("currency") or bet.currency
        bet.notes = request.form.get("notes") or bet.notes
        bet.is_freebet = request.form.get('is_freebet') == '1'
        bet.is_live = request.form.get('is_live') == '1'
        placed_at_str = request.form.get("placed_at")
        if placed_at_str:
            try:
                bet.placed_at = datetime.fromisoformat(placed_at_str)
            except ValueError:
                pass
        if form_status:
            bet.status = form_status
        legs = BetLeg.query.filter_by(bet_id=bet.id).all()
        for leg in legs:
            status_field = f"leg_status_{leg.id}"
            builder_field = f"leg_builder_{leg.id}"
            new_leg_status = request.form.get(status_field)
            if new_leg_status:
                leg.status = new_leg_status
            leg.is_builder = builder_field in request.form
        if not form_status:
            has_lost = any(leg.status == "lost" for leg in legs)
            all_won = legs and all(leg.status == "won" for leg in legs)
            if bet.status not in ("cashed_out", "void"):
                if has_lost:
                    bet.status = "lost"
                elif all_won:
                    bet.status = "won"
                else:
                    bet.status = "open"
        product = 1.0
        any_leg_odds = False
        for leg in legs:
            if leg.status == "lost":
                continue
            if leg.odds_decimal is not None:
                product *= float(leg.odds_decimal)
                any_leg_odds = True
        if any_leg_odds:
            bet.total_odds = round(product, 3)
        sync_bet_transactions(bet)
        db.session.commit()
        flash("Bet and legs updated", "success")
        return redirect(url_for("bets_list"))

    legs = BetLeg.query.filter_by(bet_id=bet.id).all()
    return render_template(
        "bet_detail.html",
        bet=bet,
        legs=legs,
        bankrolls=rolls,
        bookmakers=books,
    )

@app.route("/bets/<int:bet_id>/cashout", methods=["POST"])
@login_required
def cashout_bet(bet_id):
    bet = Bet.query.filter_by(id=bet_id, user_id=current_user.id).first_or_404()
    amount_raw = request.form.get("cashout_amount")
    try:
        amount = float(amount_raw.replace(",", "."))
    except (TypeError, ValueError):
        flash("Invalid cashout amount", "error")
        return redirect(url_for("edit_bet", bet_id=bet.id))
    bet.cashed_out_amount = amount
    bet.cashed_out_at = datetime.utcnow()
    bet.status = "cashed_out"
    db.session.commit()
    flash(f"Bet #{bet.id} cashed out for {amount}", "success")
    return redirect(url_for("edit_bet", bet_id=bet.id))

# app.py - Parte 6: Rotas de Bankrolls

@app.route("/bankrolls", methods=["GET", "POST"])
@login_required
def bankrolls_list():
    if request.method == "POST":
        name = request.form.get("name")
        currency = request.form.get("currency") or "EUR"
        starting_raw = request.form.get("starting_balance") or "0"
        bookmaker_allocations = request.form.getlist("bookmaker_allocation[]")
        bookmaker_ids = request.form.getlist("bookmaker_id[]")
        
        if not name:
            flash("Bankroll name is required.", "error")
            return redirect(url_for("bankrolls_list"))
        
        try:
            starting_balance = float(starting_raw.replace(",", "."))
        except ValueError:
            starting_balance = 0.0
        
        active_bankroll = Bankroll.query.filter_by(user_id=current_user.id, is_active=True).first()
        
        new_b = Bankroll(
            name=name,
            currency=currency,
            starting_balance=starting_balance,
            is_active=not active_bankroll,
            user_id=current_user.id
        )
        db.session.add(new_b)
        db.session.flush()
        
        total_allocated = 0
        for i, bookmaker_id in enumerate(bookmaker_ids):
            if bookmaker_id:
                amount_raw = bookmaker_allocations[i] if i < len(bookmaker_allocations) else "0"
                try:
                    amount = float(amount_raw.replace(",", "."))
                except ValueError:
                    amount = 0.0
                
                if amount > 0:
                    total_allocated += amount
                    
                    tx = Transaction(
                        bankroll_id=new_b.id,
                        type="deposit",
                        amount=amount,
                        bookmaker_id=int(bookmaker_id),
                        notes=f"Initial allocation to bookmaker"
                    )
                    db.session.add(tx)
                    
                    balance = BankrollBookmakerBalance(
                        bankroll_id=new_b.id,
                        bookmaker_id=int(bookmaker_id),
                        starting_balance=amount,
                        current_balance=amount
                    )
                    db.session.add(balance)
        
        db.session.commit()
        
        if total_allocated == 0:
            flash(f"Bankroll '{name}' created with no bookmaker allocation.", "warning")
        else:
            flash(f"Bankroll '{name}' created with {total_allocated}€ allocated to bookmakers!", "success")
        
        return redirect(url_for("bankrolls_list"))

    rolls = Bankroll.query.filter_by(user_id=current_user.id).order_by(Bankroll.created_at.asc()).all()
    books = Bookmaker.query.filter_by(user_id=current_user.id).order_by(Bookmaker.name.asc()).all()
    
    current_balances = {}
    total_balance = 0
    total_profit = 0
    total_invested = 0
    total_withdrawn = 0
    
    bankrolls_by_month = {}
    
    for roll in rolls:
        net = sum(
            tx.amount if tx.type == "deposit" else -tx.amount
            for tx in roll.transactions
        )
        current_balance = roll.starting_balance + net
        current_balances[roll.id] = round(current_balance, 2)
        total_balance += current_balance
        
        bets = Bet.query.filter_by(bankroll_id=roll.id).all()
        profit = 0
        invested = 0
        
        for bet in bets:
            if bet.stake and not bet.is_freebet:
                invested += bet.stake
            if bet.is_freebet:
                if bet.status == "won" and bet.potential_return:
                    profit += bet.potential_return
                elif bet.status == "cashed_out" and bet.cashed_out_amount:
                    profit += bet.cashed_out_amount
            else:
                if bet.status == "won" and bet.potential_return and bet.stake:
                    profit += bet.potential_return - bet.stake
                elif bet.status == "lost" and bet.stake:
                    profit -= bet.stake
                elif bet.status == "cashed_out" and bet.cashed_out_amount and bet.stake:
                    profit += bet.cashed_out_amount - bet.stake
        
        total_profit += profit
        total_invested += invested
        
        for tx in roll.transactions:
            if tx.type == "withdrawal":
                total_withdrawn += tx.amount
        
        month_key = roll.created_at.strftime("%B %Y") if roll.created_at else "Unknown"
        if month_key not in bankrolls_by_month:
            bankrolls_by_month[month_key] = {
                "bankrolls": [],
                "total_profit": 0
            }
        
        bets_count = len(bets)
        won_bets = sum(1 for b in bets if b.status == "won")
        win_rate = (won_bets / bets_count * 100) if bets_count > 0 else 0
        avg_odds = sum(b.total_odds or 0 for b in bets) / bets_count if bets_count > 0 else 0
        active_stake = sum(b.stake or 0 for b in bets if b.status == "open")
        free_bets_count = sum(1 for b in bets if b.is_freebet)
        
        bankroll_data = {
            "id": roll.id,
            "name": roll.name,
            "starting_balance": roll.starting_balance,
            "current_balance": current_balance,
            "bets_count": bets_count,
            "win_rate": win_rate,
            "avg_odds": avg_odds,
            "active_stake": active_stake,
            "free_bets_count": free_bets_count,
            "profit": profit,
            "is_active": roll.is_active
        }
        
        bankrolls_by_month[month_key]["bankrolls"].append(bankroll_data)
        bankrolls_by_month[month_key]["total_profit"] += profit

    month_order = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    sorted_months = sorted(
        bankrolls_by_month.keys(),
        key=lambda x: (month_order.index(x.split()[0]) if x.split()[0] in month_order else 0, x.split()[1] if len(x.split()) > 1 else "2020"),
        reverse=True
    )
    
    sorted_bankrolls_by_month = {}
    for month in sorted_months:
        sorted_bankrolls_by_month[month] = bankrolls_by_month[month]

    return render_template(
        "bankrolls.html",
        bankrolls=rolls,
        bookmakers=books,
        current_balances=current_balances,
        total_balance=round(total_balance, 2),
        total_profit=round(total_profit, 2),
        total_invested=round(total_invested, 2),
        total_withdrawn=round(total_withdrawn, 2),
        bankrolls_by_month=sorted_bankrolls_by_month,
    )

@app.route("/bankrolls/<int:roll_id>/edit", methods=["GET", "POST"])
@login_required
def edit_bankroll(roll_id):
    roll = Bankroll.query.filter_by(id=roll_id, user_id=current_user.id).first_or_404()
    if request.method == "POST":
        name = request.form.get("name")
        currency = request.form.get("currency")
        starting_raw = request.form.get("starting_balance") or "0"
        if not name:
            flash("Bankroll name is required.", "error")
            return redirect(url_for("edit_bankroll", roll_id=roll.id))
        try:
            starting_balance = float(starting_raw.replace(",", "."))
        except ValueError:
            starting_balance = 0.0
        roll.name = name
        roll.currency = currency
        roll.starting_balance = starting_balance
        db.session.commit()
        flash("Bankroll updated successfully!", "success")
        return redirect(url_for("bankrolls_list"))
    return render_template("edit_bankroll.html", bankroll=roll)

@app.route("/bankrolls/<int:roll_id>/delete", methods=["POST"])
@login_required
def delete_bankroll(roll_id):
    roll = Bankroll.query.filter_by(id=roll_id, user_id=current_user.id).first_or_404()
    bets_count = Bet.query.filter_by(bankroll_id=roll.id).count()
    if bets_count > 0:
        flash(f"Cannot delete bankroll '{roll.name}' because it has {bets_count} associated bets.", "error")
        return redirect(url_for("bankrolls_list"))
    transactions_count = Transaction.query.filter_by(bankroll_id=roll.id).count()
    if transactions_count > 0:
        Transaction.query.filter_by(bankroll_id=roll.id).delete()
    balances_count = BankrollBookmakerBalance.query.filter_by(bankroll_id=roll.id).count()
    if balances_count > 0:
        BankrollBookmakerBalance.query.filter_by(bankroll_id=roll.id).delete()
    if roll.is_active:
        roll.is_active = False
        db.session.commit()
    roll_name = roll.name
    db.session.delete(roll)
    db.session.commit()
    remaining = Bankroll.query.filter_by(user_id=current_user.id).first()
    if remaining:
        remaining.is_active = True
        db.session.commit()
    flash(f"Bankroll '{roll_name}' deleted successfully!", "success")
    return redirect(url_for("bankrolls_list"))

@app.route("/bankrolls/<int:roll_id>/set_active", methods=["POST"])
@login_required
def set_active_bankroll(roll_id):
    Bankroll.query.filter_by(user_id=current_user.id).update({Bankroll.is_active: False})
    db.session.commit()
    roll = Bankroll.query.filter_by(id=roll_id, user_id=current_user.id).first_or_404()
    roll.is_active = True
    db.session.commit()
    flash(f"Bankroll '{roll.name}' set as active!", "success")
    referrer = request.referrer
    if referrer:
        return redirect(referrer)
    return redirect(url_for("bankrolls_list"))

@app.route("/bankrolls/<int:roll_id>/deposit", methods=["GET", "POST"])
@login_required
def deposit_bankroll(roll_id):
    roll = Bankroll.query.filter_by(id=roll_id, user_id=current_user.id).first_or_404()
    books = Bookmaker.query.filter_by(user_id=current_user.id).order_by(Bookmaker.name.asc()).all()
    if request.method == "POST":
        amount_raw = request.form.get("amount")
        bookmaker_id = request.form.get("bookmaker_id")
        notes = request.form.get("notes") or f"Deposit to {roll.name}"
        try:
            amount = float(amount_raw.replace(",", "."))
        except (TypeError, ValueError):
            flash("Invalid amount", "error")
            return redirect(url_for("deposit_bankroll", roll_id=roll.id))
        if amount <= 0:
            flash("Amount must be greater than zero", "error")
            return redirect(url_for("deposit_bankroll", roll_id=roll.id))
        tx = Transaction(
            bankroll_id=roll.id,
            type="deposit",
            amount=amount,
            notes=notes,
            bookmaker_id=int(bookmaker_id) if bookmaker_id else None,
        )
        db.session.add(tx)
        db.session.commit()
        flash(f"Deposit of {amount} {roll.currency} recorded successfully!", "success")
        return redirect(url_for("bankrolls_list"))
    return render_template("deposit_bankroll.html", bankroll=roll, bookmakers=books)

@app.route("/bankrolls/<int:roll_id>/withdraw", methods=["GET", "POST"])
@login_required
def withdraw_bankroll(roll_id):
    roll = Bankroll.query.filter_by(id=roll_id, user_id=current_user.id).first_or_404()
    books = Bookmaker.query.filter_by(user_id=current_user.id).order_by(Bookmaker.name.asc()).all()
    net = sum(tx.amount if tx.type == "deposit" else -tx.amount for tx in roll.transactions)
    current_balance = roll.starting_balance + net
    if request.method == "POST":
        amount_raw = request.form.get("amount")
        bookmaker_id = request.form.get("bookmaker_id")
        notes = request.form.get("notes") or f"Withdrawal from {roll.name}"
        try:
            amount = float(amount_raw.replace(",", "."))
        except (TypeError, ValueError):
            flash("Invalid amount", "error")
            return redirect(url_for("withdraw_bankroll", roll_id=roll.id))
        if amount <= 0:
            flash("Amount must be greater than zero", "error")
            return redirect(url_for("withdraw_bankroll", roll_id=roll.id))
        if amount > current_balance:
            flash(f"Insufficient balance. Available: {current_balance} {roll.currency}", "error")
            return redirect(url_for("withdraw_bankroll", roll_id=roll.id))
        tx = Transaction(
            bankroll_id=roll.id,
            type="withdrawal",
            amount=amount,
            notes=notes,
            bookmaker_id=int(bookmaker_id) if bookmaker_id else None,
        )
        db.session.add(tx)
        db.session.commit()
        flash(f"Withdrawal of {amount} {roll.currency} recorded successfully!", "success")
        return redirect(url_for("bankrolls_list"))
    return render_template(
        "withdraw_bankroll.html", 
        bankroll=roll, 
        bookmakers=books,
        current_balance=current_balance
    )

@app.route("/transfer_funds", methods=["POST"])
@login_required
def transfer_funds():
    from_bankroll_id = request.form.get("from_bankroll_id")
    to_bankroll_id = request.form.get("to_bankroll_id")
    amount_raw = request.form.get("amount")
    notes = request.form.get("notes") or "Transfer between bankrolls"
    if not from_bankroll_id or not to_bankroll_id:
        flash("Please select both bankrolls", "error")
        return redirect(request.referrer or url_for("bankrolls_list"))
    if from_bankroll_id == to_bankroll_id:
        flash("Cannot transfer to the same bankroll", "error")
        return redirect(request.referrer or url_for("bankrolls_list"))
    try:
        amount = float(amount_raw.replace(",", "."))
    except (TypeError, ValueError):
        flash("Invalid amount", "error")
        return redirect(request.referrer or url_for("bankrolls_list"))
    if amount <= 0:
        flash("Amount must be greater than zero", "error")
        return redirect(request.referrer or url_for("bankrolls_list"))
    from_bankroll = Bankroll.query.filter_by(id=from_bankroll_id, user_id=current_user.id).first_or_404()
    to_bankroll = Bankroll.query.filter_by(id=to_bankroll_id, user_id=current_user.id).first_or_404()
    net = sum(tx.amount if tx.type == "deposit" else -tx.amount for tx in from_bankroll.transactions)
    current_balance = from_bankroll.starting_balance + net
    if amount > current_balance:
        flash(f"Insufficient balance in {from_bankroll.name}. Available: {current_balance} {from_bankroll.currency}", "error")
        return redirect(request.referrer or url_for("bankrolls_list"))
    tx_out = Transaction(
        bankroll_id=from_bankroll.id,
        type="withdrawal",
        amount=amount,
        notes=f"Transfer to {to_bankroll.name}: {notes}",
    )
    db.session.add(tx_out)
    tx_in = Transaction(
        bankroll_id=to_bankroll.id,
        type="deposit",
        amount=amount,
        notes=f"Transfer from {from_bankroll.name}: {notes}",
    )
    db.session.add(tx_in)
    db.session.commit()
    flash(f"Transferred {amount}€ from {from_bankroll.name} to {to_bankroll.name}", "success")
    return redirect(request.referrer or url_for("bankrolls_list"))

@app.route("/bankrolls/<int:roll_id>/manage", methods=["GET", "POST"])
@login_required
def manage_funds(roll_id):
    roll = Bankroll.query.filter_by(id=roll_id, user_id=current_user.id).first_or_404()
    books = Bookmaker.query.filter_by(user_id=current_user.id).order_by(Bookmaker.name.asc()).all()
    
    # Calcular saldo atual do bankroll
    net = sum(
        tx.amount if tx.type == "deposit" else -tx.amount
        for tx in roll.transactions
    )
    current_balance = roll.starting_balance + net
    
    # Balance nunca pode ser negativo
    if current_balance < 0:
        current_balance = 0.0
    
    # Calcular estatísticas
    bets = Bet.query.filter_by(bankroll_id=roll.id).all()
    bets_count = len(bets)
    won_bets = sum(1 for b in bets if b.status == "won")
    lost_bets = sum(1 for b in bets if b.status == "lost")
    win_rate = (won_bets / bets_count * 100) if bets_count > 0 else 0
    
    total_staked = sum(b.stake or 0 for b in bets)
    total_profit = 0
    for b in bets:
        if b.is_freebet:
            if b.status == "won" and b.potential_return:
                total_profit += b.potential_return
            elif b.status == "cashed_out" and b.cashed_out_amount:
                total_profit += b.cashed_out_amount
        else:
            if b.status == "won" and b.potential_return and b.stake:
                total_profit += b.potential_return - b.stake
            elif b.status == "lost" and b.stake:
                total_profit -= b.stake
            elif b.status == "cashed_out" and b.cashed_out_amount and b.stake:
                total_profit += b.cashed_out_amount - b.stake
    
    yield_pct = (total_profit / total_staked * 100) if total_staked > 0 else 0
    
    avg_odds = sum(b.total_odds or 0 for b in bets) / bets_count if bets_count > 0 else 0
    avg_stake = total_staked / bets_count if bets_count > 0 else 0
    
    # Melhor e pior sequência
    streak = 0
    best_streak = 0
    worst_streak = 0
    current_streak = 0
    
    for b in sorted(bets, key=lambda x: x.placed_at or datetime.min):
        if b.status == "won":
            streak = streak + 1 if streak >= 0 else 1
        elif b.status == "lost":
            streak = streak - 1 if streak <= 0 else -1
        else:
            continue
        
        if streak > best_streak:
            best_streak = streak
        if streak < worst_streak:
            worst_streak = streak
    
    current_streak = streak
    
    # ===== ESTATÍSTICAS POR BOOKMAKER =====
    # SIMPLESMENTE USAR O CURRENT_BALANCE GUARDADO - NÃO RECALCULAR
    bookmaker_balances = {}
    for book in books:
        balance_record = BankrollBookmakerBalance.query.filter_by(
            bankroll_id=roll.id,
            bookmaker_id=book.id
        ).first()
        
        if balance_record:
            # USAR O VALOR GUARDADO
            bookmaker_balances[book.id] = round(balance_record.current_balance, 2)
        else:
            # Criar balance record se não existir
            new_balance = BankrollBookmakerBalance(
                bankroll_id=roll.id,
                bookmaker_id=book.id,
                starting_balance=0.0,
                current_balance=0.0
            )
            db.session.add(new_balance)
            db.session.commit()
            bookmaker_balances[book.id] = 0.0
    
    # Estatísticas de apostas por tipo
    simple_bets = [b for b in bets if b.market_type and "Combined" not in b.market_type]
    combined_bets = [b for b in bets if b.market_type and "Combined" in b.market_type]
    
    simple_profit = 0
    for b in simple_bets:
        if b.is_freebet:
            if b.status == "won" and b.potential_return:
                simple_profit += b.potential_return
        else:
            if b.status == "won" and b.potential_return and b.stake:
                simple_profit += b.potential_return - b.stake
            elif b.status == "lost" and b.stake:
                simple_profit -= b.stake
    
    combined_profit = 0
    for b in combined_bets:
        if b.is_freebet:
            if b.status == "won" and b.potential_return:
                combined_profit += b.potential_return
        else:
            if b.status == "won" and b.potential_return and b.stake:
                combined_profit += b.potential_return - b.stake
            elif b.status == "lost" and b.stake:
                combined_profit -= b.stake
    
    simple_won = sum(1 for b in simple_bets if b.status == "won")
    simple_lost = sum(1 for b in simple_bets if b.status == "lost")
    combined_won = sum(1 for b in combined_bets if b.status == "won")
    combined_lost = sum(1 for b in combined_bets if b.status == "lost")
    
    # Estatísticas por desporto (apenas para apostas simples)
    sport_stats = {}
    for b in simple_bets:
        key = b.sport or "Desconhecido"
        sport_stats.setdefault(key, {"staked": 0.0, "profit": 0.0, "count": 0})
        sport_stats[key]["staked"] += b.stake or 0.0
        if b.is_freebet:
            if b.status == "won" and b.potential_return:
                sport_stats[key]["profit"] += b.potential_return
        else:
            if b.status == "won" and b.potential_return and b.stake:
                sport_stats[key]["profit"] += b.potential_return - b.stake
            elif b.status == "lost" and b.stake:
                sport_stats[key]["profit"] -= b.stake
        sport_stats[key]["count"] += 1
    
    # Processar POST (depósito/levantamento)
    if request.method == "POST":
        tx_type = request.form.get("type")
        amount_raw = request.form.get("amount")
        bookmaker_id = request.form.get("bookmaker_id")
        notes = request.form.get("notes")
        
        try:
            amount = float(amount_raw.replace(",", "."))
        except (TypeError, ValueError):
            flash("Invalid amount", "error")
            return redirect(url_for("manage_funds", roll_id=roll.id))
        
        if amount <= 0:
            flash("Amount must be greater than zero", "error")
            return redirect(url_for("manage_funds", roll_id=roll.id))
        
        if tx_type == "withdrawal":
            # Verificar se há saldo suficiente no bankroll
            if amount > current_balance:
                flash(f"Insufficient balance. Available: {current_balance} {roll.currency}", "error")
                return redirect(url_for("manage_funds", roll_id=roll.id))
            
            # Verificar se há saldo suficiente no bookmaker selecionado
            if bookmaker_id:
                balance_record = BankrollBookmakerBalance.query.filter_by(
                    bankroll_id=roll.id,
                    bookmaker_id=int(bookmaker_id)
                ).first()
                
                if balance_record and balance_record.current_balance < amount:
                    flash(f"Insufficient balance in this bookmaker. Available: {balance_record.current_balance} {roll.currency}", "error")
                    return redirect(url_for("manage_funds", roll_id=roll.id))
        
        # Criar transação
        tx = Transaction(
            bankroll_id=roll.id,
            type=tx_type,
            amount=amount,
            notes=notes,
            bookmaker_id=int(bookmaker_id) if bookmaker_id else None,
        )
        db.session.add(tx)
        db.session.commit()
        
        # ===== ATUALIZAR O BALANCE DO BOOKMAKER =====
        if bookmaker_id:
            update_bookmaker_balance_from_transactions(roll.id, int(bookmaker_id))
        
        flash(f"{tx_type.capitalize()} of {amount} {roll.currency} recorded successfully!", "success")
        return redirect(url_for("manage_funds", roll_id=roll.id))
    
    # Últimas 5 transações
    recent_transactions = Transaction.query.filter_by(bankroll_id=roll.id).order_by(Transaction.created_at.desc()).limit(5).all()
    
    # Últimas 5 apostas
    recent_bets = Bet.query.filter_by(bankroll_id=roll.id).order_by(Bet.placed_at.desc()).limit(5).all()
    
    # Dados do gráfico (profit acumulado)
    chart_labels = []
    chart_data = []
    cumulative = 0
    sorted_bets = sorted(bets, key=lambda x: x.placed_at or datetime.min)
    for b in sorted_bets:
        if b.is_freebet:
            if b.status == "won" and b.potential_return:
                cumulative += b.potential_return
            elif b.status == "cashed_out" and b.cashed_out_amount:
                cumulative += b.cashed_out_amount
        else:
            if b.status == "won" and b.potential_return and b.stake:
                cumulative += b.potential_return - b.stake
            elif b.status == "lost" and b.stake:
                cumulative -= b.stake
            elif b.status == "cashed_out" and b.cashed_out_amount and b.stake:
                cumulative += b.cashed_out_amount - b.stake
        chart_labels.append(b.placed_at.strftime("%d/%m") if b.placed_at else "N/A")
        chart_data.append(round(cumulative, 2))
    
    # Obter todos os bankrolls para o modal de transferência (excluindo o atual)
    all_bankrolls = Bankroll.query.filter(Bankroll.id != roll.id, Bankroll.user_id == current_user.id).all()
    
    return render_template(
        "manage_funds.html",
        bankroll=roll,
        bookmakers=books,
        all_bankrolls=all_bankrolls,
        current_balance=round(current_balance, 2),
        total_staked=round(total_staked, 2),
        total_profit=round(total_profit, 2),
        win_rate=round(win_rate, 1),
        yield_pct=round(yield_pct, 2),
        avg_odds=round(avg_odds, 2),
        avg_stake=round(avg_stake, 2),
        best_streak=best_streak,
        worst_streak=worst_streak,
        current_streak=current_streak,
        bets_count=bets_count,
        won_bets=won_bets,
        lost_bets=lost_bets,
        bookmaker_balances=bookmaker_balances,
        recent_transactions=recent_transactions,
        recent_bets=recent_bets,
        simple_won=simple_won,
        simple_lost=simple_lost,
        combined_won=combined_won,
        combined_lost=combined_lost,
        simple_profit=round(simple_profit, 2),
        combined_profit=round(combined_profit, 2),
        sport_stats=sport_stats,
        chart_labels=json.dumps(chart_labels),
        chart_data=json.dumps(chart_data),
    )

def update_bookmaker_balance_from_transactions(bankroll_id, bookmaker_id):
    """Atualiza o current_balance baseado nas transações (apenas quando há novas transações)"""
    balance_record = BankrollBookmakerBalance.query.filter_by(
        bankroll_id=bankroll_id,
        bookmaker_id=bookmaker_id
    ).first()
    
    if not balance_record:
        balance_record = BankrollBookmakerBalance(
            bankroll_id=bankroll_id,
            bookmaker_id=bookmaker_id,
            starting_balance=0.0,
            current_balance=0.0
        )
        db.session.add(balance_record)
        db.session.flush()
    
    roll = Bankroll.query.get(bankroll_id)
    
    deposits = sum(
        tx.amount for tx in roll.transactions
        if tx.bookmaker_id == bookmaker_id and tx.type == "deposit"
    )
    withdrawals = sum(
        tx.amount for tx in roll.transactions
        if tx.bookmaker_id == bookmaker_id and tx.type == "withdrawal"
    )
    
    # Calcular impacto das apostas
    bets = Bet.query.filter_by(
        bookmaker_id=bookmaker_id,
        bankroll_id=bankroll_id
    ).all()
    
    bets_impact = 0.0
    for bet in bets:
        if bet.is_freebet:
            if bet.status == "won" and bet.potential_return:
                bets_impact += bet.potential_return
            elif bet.status == "cashed_out" and bet.cashed_out_amount:
                bets_impact += bet.cashed_out_amount
        else:
            if bet.status == "won" and bet.potential_return and bet.stake:
                bets_impact += bet.potential_return - bet.stake
            elif bet.status == "lost" and bet.stake:
                bets_impact -= bet.stake
            elif bet.status == "cashed_out" and bet.cashed_out_amount and bet.stake:
                bets_impact += bet.cashed_out_amount - bet.stake
    
    current = balance_record.starting_balance + deposits - withdrawals + bets_impact
    
    if current < 0:
        current = 0.0
    
    balance_record.current_balance = current
    db.session.commit()
    
    return balance_record

# app.py - Parte 7: Rotas de Bookmakers

@app.route("/bookmakers", methods=["GET", "POST"])
@login_required
def bookmakers_list():
    if request.method == "POST":
        name = request.form.get("name")
        currency = request.form.get("currency") or "EUR"
        
        if not name:
            flash("Bookmaker name is required.", "error")
            return redirect(url_for("bookmakers_list"))
        
        new_bm = Bookmaker(
            name=name,
            currency=currency,
            starting_balance=0.0,
            user_id=current_user.id
        )
        db.session.add(new_bm)
        db.session.commit()
        flash("Bookmaker added!", "success")
        return redirect(url_for("bookmakers_list"))

    books = Bookmaker.query.filter_by(user_id=current_user.id).order_by(Bookmaker.name.asc()).all()
    all_bankrolls = Bankroll.query.filter_by(user_id=current_user.id).order_by(Bankroll.name.asc()).all()
    active_bankroll = Bankroll.query.filter_by(user_id=current_user.id, is_active=True).first()
    selected_bankroll_id = request.args.get('bankroll_id', '')
    
    if selected_bankroll_id and selected_bankroll_id.isdigit():
        target_bankroll = Bankroll.query.get(int(selected_bankroll_id))
    else:
        target_bankroll = active_bankroll
    
    # ===== CALCULAR BALANÇO POR BOOKMAKER =====
    # USAR O CURRENT_BALANCE GUARDADO - NÃO RECALCULAR
    balances = {}
    for book in books:
        if target_bankroll:
            balance_record = BankrollBookmakerBalance.query.filter_by(
                bankroll_id=target_bankroll.id,
                bookmaker_id=book.id
            ).first()
            
            if balance_record:
                # USAR O VALOR GUARDADO
                balances[book.id] = round(balance_record.current_balance, 2)
            else:
                # Criar balance record se não existir
                new_balance = BankrollBookmakerBalance(
                    bankroll_id=target_bankroll.id,
                    bookmaker_id=book.id,
                    starting_balance=0.0,
                    current_balance=0.0
                )
                db.session.add(new_balance)
                db.session.commit()
                balances[book.id] = 0.0
        else:
            balances[book.id] = 0.0

    return render_template(
        "bookmakers.html",
        bookmakers=books,
        balances=balances,
        active_bankroll=target_bankroll,
        all_bankrolls=all_bankrolls,
        selected_bankroll_id=selected_bankroll_id
    )

@app.route("/bookmakers/<int:book_id>/edit", methods=["GET", "POST"])
@login_required
def edit_bookmaker(book_id):
    book = Bookmaker.query.filter_by(id=book_id, user_id=current_user.id).first_or_404()
    bankroll_id = request.args.get('bankroll_id')
    
    if request.method == "POST":
        name = request.form.get("name")
        bankroll_id = request.form.get("bankroll_id")
        starting_balance_raw = request.form.get("starting_balance") or "0"
        currency = request.form.get("currency") or "EUR"
        if not name:
            flash("Bookmaker name is required.", "error")
            return redirect(url_for("edit_bookmaker", book_id=book.id, bankroll_id=bankroll_id))
        try:
            starting_balance = float(starting_balance_raw.replace(",", "."))
        except ValueError:
            starting_balance = 0.0
        book.name = name
        book.currency = currency
        db.session.commit()
        if bankroll_id:
            balance_record = BankrollBookmakerBalance.query.filter_by(
                bankroll_id=int(bankroll_id),
                bookmaker_id=book.id
            ).first()
            if balance_record:
                old_starting = balance_record.starting_balance
                old_current = balance_record.current_balance
                balance_record.starting_balance = starting_balance
                if old_current == old_starting:
                    balance_record.current_balance = starting_balance
                db.session.commit()
                flash(f"Bookmaker '{book.name}' updated with starting balance {starting_balance:.2f}€ for this bankroll!", "success")
            else:
                new_balance = BankrollBookmakerBalance(
                    bankroll_id=int(bankroll_id),
                    bookmaker_id=book.id,
                    starting_balance=starting_balance,
                    current_balance=starting_balance
                )
                db.session.add(new_balance)
                db.session.commit()
                flash(f"Bookmaker '{book.name}' added to bankroll with starting balance {starting_balance:.2f}€!", "success")
        else:
            flash(f"Bookmaker '{book.name}' updated! (no bankroll selected)", "success")
        return redirect(url_for("bookmakers_list", bankroll_id=bankroll_id))
    
    current_starting = 0.0
    if bankroll_id:
        balance_record = BankrollBookmakerBalance.query.filter_by(
            bankroll_id=int(bankroll_id),
            bookmaker_id=book.id
        ).first()
        if balance_record:
            current_starting = balance_record.starting_balance
    
    all_bankrolls = Bankroll.query.filter_by(user_id=current_user.id).order_by(Bankroll.name.asc()).all()
    return render_template(
        "edit_bookmaker.html", 
        bookmaker=book,
        all_bankrolls=all_bankrolls,
        selected_bankroll_id=bankroll_id,
        current_starting=current_starting
    )

@app.route("/bookmakers/<int:book_id>/delete", methods=["POST"])
@login_required
def delete_bookmaker(book_id):
    book = Bookmaker.query.filter_by(id=book_id, user_id=current_user.id).first_or_404()
    db.session.delete(book)
    db.session.commit()
    flash(f"Bookmaker '{book.name}' deleted!", "success")
    return redirect(url_for("bookmakers_list"))

@app.route("/bookmakers/<int:book_id>/update_balance", methods=["POST"])
@login_required
def update_bookmaker_balance_manual(book_id):
    book = Bookmaker.query.filter_by(id=book_id, user_id=current_user.id).first_or_404()
    bankroll_id = request.form.get("bankroll_id")
    new_balance_raw = request.form.get("new_balance")
    notes = request.form.get("notes") or f"Manual balance adjustment for {book.name}"
    
    if not bankroll_id:
        flash("Bankroll is required.", "error")
        return redirect(url_for("bookmakers_list"))
    
    try:
        new_balance = float(new_balance_raw.replace(",", "."))
    except (TypeError, ValueError):
        flash("Invalid balance value.", "error")
        return redirect(url_for("bookmakers_list"))
    
    if new_balance < 0:
        flash("Balance cannot be negative.", "error")
        return redirect(url_for("bookmakers_list"))
    
    # Buscar o balance record
    balance_record = BankrollBookmakerBalance.query.filter_by(
        bankroll_id=int(bankroll_id),
        bookmaker_id=book_id
    ).first()
    
    if not balance_record:
        # Criar se não existir
        balance_record = BankrollBookmakerBalance(
            bankroll_id=int(bankroll_id),
            bookmaker_id=book_id,
            starting_balance=0.0,
            current_balance=0.0
        )
        db.session.add(balance_record)
        db.session.flush()
    
    current_balance = balance_record.current_balance
    
    if new_balance == current_balance:
        flash("No change in balance.", "info")
        return redirect(url_for("bookmakers_list", bankroll_id=bankroll_id))
    
    # ATUALIZAR DIRETAMENTE O CURRENT_BALANCE
    # NÃO criar transação - apenas substituir o valor
    balance_record.current_balance = new_balance
    db.session.commit()
    
    flash(f"Balance for {book.name} corrected from {current_balance:.2f}€ to {new_balance:.2f}€!", "success")
    return redirect(url_for("bookmakers_list", bankroll_id=bankroll_id))

# app.py - Parte 8: Rotas de Upload, Stats, Tips, Export, Import e Admin

# ===== UPLOAD =====
@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        # ===== DEBUG =====
        print("=" * 50)
        print("📤 UPLOAD - DEBUG")
        print(f"   - GEMINI_API_KEY: {'✅' if GEMINI_API_KEY else '❌'}")
        print(f"   - UPLOAD_FOLDER: {UPLOAD_FOLDER}")
        print("=" * 50)
        
        file = request.files.get("image")
        if not file or file.filename == "":
            flash("No file uploaded", "error")
            return redirect(request.url)

        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}_{file.filename}"
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)
        
        # ===== VERIFICAR SE O FICHEIRO FOI GUARDADO =====
        if not os.path.exists(filepath):
            print(f"❌ Ficheiro NÃO guardado: {filepath}")
            flash("Erro ao guardar a imagem", "error")
            return redirect(request.url)
        
        file_size = os.path.getsize(filepath)
        print(f"✅ Ficheiro guardado: {filepath} ({file_size} bytes)")
        
        if file_size == 0:
            flash("Ficheiro vazio - tenta novamente", "error")
            return redirect(request.url)

        is_freebet = request.form.get('is_freebet') == 'on'
        is_live = request.form.get('is_live') == 'on'
        bankroll_id = request.form.get('bankroll_id')
        
        if not bankroll_id:
            active = get_active_bankroll()
            if active:
                bankroll_id = str(active.id)
                
        bookmaker_id = request.form.get('bookmaker_id') or request.form.get('bookmaker_id_combined')
        
        try:
            print("🔄 Chamando Gemini API...")
            gemini_data = call_gemini_on_betslip(filepath)
            print("✅ Gemini respondeu com sucesso!")
        except Exception as e:
            import traceback
            print(f"❌ Erro no Gemini: {e}")
            print(traceback.format_exc())
            flash(f"Error reading betslip with AI: {e}", "error")
            bet = Bet(
                id=get_next_bet_id(),
                image_path=filename,
                status="open",
                notes=f"AI parsing failed: {e}",
                is_freebet=is_freebet,
                is_live=is_live,
                bankroll_id=int(bankroll_id) if bankroll_id else None,
                bookmaker_id=int(bookmaker_id) if bookmaker_id else None,
                placed_at=datetime.utcnow(),
                user_id=current_user.id
            )
            db.session.add(bet)
            db.session.commit()
            flash(f"Bet #{bet.id} created with errors. You can edit it manually.", "warning")
            return redirect(url_for("edit_bet", bet_id=bet.id))
        
        parsed = parse_betslip_from_gemini(gemini_data)
        print(f"📊 Dados extraídos: {parsed}")

        # ---- Lógica de Bookmaker ----
        if bookmaker_id:
            try:
                matched_bookmaker_id = int(bookmaker_id)
                raw_bookmaker_name = Bookmaker.query.get(matched_bookmaker_id).name if matched_bookmaker_id else None
            except ValueError:
                matched_bookmaker_id = None
                raw_bookmaker_name = None
        else:
            raw_bookmaker_name = parsed.get("bookmaker")
            matched_bookmaker_id = None
            if raw_bookmaker_name:
                clean_name = raw_bookmaker_name.strip()
                existing_bookmaker = Bookmaker.query.filter(
                    Bookmaker.name.ilike(clean_name),
                    Bookmaker.user_id == current_user.id
                ).first()
                if existing_bookmaker:
                    matched_bookmaker_id = existing_bookmaker.id
                else:
                    new_book = Bookmaker(name=clean_name, user_id=current_user.id)
                    db.session.add(new_book)
                    db.session.flush()
                    matched_bookmaker_id = new_book.id

        stake = parsed.get("stake")
        total_odds = parsed.get("total_odds")
        potential_return = round(stake * total_odds, 2) if (stake and total_odds) else None

        placed_at = parsed.get("placed_at")
        if not placed_at:
            placed_at = datetime.utcnow()
        else:
            try:
                if hasattr(placed_at, 'year') and (placed_at.year < 2020 or placed_at.year > 2030):
                    placed_at = datetime.utcnow()
            except AttributeError:
                placed_at = datetime.utcnow()

        bet = Bet(
            id=get_next_bet_id(),
            bookmaker=raw_bookmaker_name,
            bookmaker_id=matched_bookmaker_id,
            bankroll_id=int(bankroll_id) if bankroll_id else None,
            sport=parsed.get("sport"),
            market_type=parsed.get("market_type"),
            total_odds=total_odds,
            stake=stake,
            potential_return=potential_return,
            currency=parsed.get("currency") or "EUR",
            status=parsed.get("status") or "open",
            image_path=filename,
            raw_json=str(gemini_data),
            placed_at=placed_at,
            notes=f"Bet ID: {parsed.get('bet_id')}" if parsed.get("bet_id") else None,
            is_freebet=is_freebet,
            is_live=is_live,
            user_id=current_user.id
        )
        db.session.add(bet)
        db.session.flush()

        legs = parsed.get("legs") or []
        for leg_data in legs:
            leg = BetLeg(
                bet_id=bet.id,
                event=leg_data.get("event"),
                team=leg_data.get("team"),
                market=leg_data.get("market"),
                odds_decimal=leg_data.get("odds_decimal"),
            )
            db.session.add(leg)

        db.session.commit()
        flash(f"✅ Bet #{bet.id} uploaded! Bookmaker: {raw_bookmaker_name or 'AI detected'}, Freebet: {is_freebet}", "success")
        return redirect(url_for("edit_bet", bet_id=bet.id))
    
    # GET - renderizar o formulário
    bankrolls = Bankroll.query.filter_by(user_id=current_user.id).all()
    bookmakers = Bookmaker.query.filter_by(user_id=current_user.id).all()
    active_bankroll = get_active_bankroll()
    
    return render_template("upload.html", 
                          bankrolls=bankrolls, 
                          bookmakers=bookmakers,
                          active_bankroll=active_bankroll)
    
# ===== STATS =====
@app.route("/stats")
@login_required
def stats():
    period = request.args.get("period", "all")
    selected_bankroll = request.args.get("bankroll_id", "")
    selected_sport = request.args.get("sport", "")
    if not selected_bankroll:
        active = get_active_bankroll()
        if active:
            selected_bankroll = str(active.id)
    query = Bet.query.filter_by(user_id=current_user.id)
    now = datetime.utcnow()
    if period == "7d":
        query = query.filter(Bet.placed_at >= now - timedelta(days=7))
    elif period == "30d":
        query = query.filter(Bet.placed_at >= now - timedelta(days=30))
    elif period == "this_month":
        first_of_month = datetime(now.year, now.month, 1)
        query = query.filter(Bet.placed_at >= first_of_month)
    elif period == "this_year":
        first_of_year = datetime(now.year, 1, 1)
        query = query.filter(Bet.placed_at >= first_of_year)
    if selected_bankroll and selected_bankroll.isdigit():
        query = query.filter(Bet.bankroll_id == int(selected_bankroll))
    if selected_sport:
        query = query.filter(Bet.sport == selected_sport)
    bets = query.all()
    sorted_bets = sorted(bets, key=lambda x: x.placed_at or datetime.min)
    all_recent_bets = sorted(bets, key=lambda x: x.placed_at or datetime.min, reverse=True)
    
    total_bets_count = len(bets)
    total_staked = sum(b.stake or 0.0 for b in bets)
    won_bets = [b for b in bets if b.status == "won"]
    lost_bets = [b for b in bets if b.status == "lost"]
    cashed_out_bets = [b for b in bets if b.status == "cashed_out"]
    resolved_bets_count = len(won_bets) + len(lost_bets) + len(cashed_out_bets)
    win_rate = (len(won_bets) / resolved_bets_count * 100) if resolved_bets_count > 0 else 0.0
    valid_odds = [b.total_odds for b in bets if b.total_odds is not None]
    avg_odds = (sum(valid_odds) / len(valid_odds)) if valid_odds else 0.0
    
    total_profit = 0.0
    chart_labels = []
    chart_data = []
    cumulative_profit = 0.0
    for b in sorted_bets:
        p = bet_profit(b)
        total_profit += p
        cumulative_profit += p
        chart_labels.append(b.placed_at.strftime("%d/%m") if b.placed_at else "N/A")
        chart_data.append(round(cumulative_profit, 2))
    roi = (total_profit / total_staked * 100) if total_staked > 0 else 0.0
    
    def calculate_drawdown(bets_list):
        peak = 0
        drawdown = 0
        cumulative = 0
        for b in sorted(bets_list, key=lambda x: x.placed_at or datetime.min):
            if b.is_freebet:
                if b.status == "won" and b.potential_return:
                    cumulative += b.potential_return
                elif b.status == "cashed_out" and b.cashed_out_amount:
                    cumulative += b.cashed_out_amount
            else:
                if b.status == "won" and b.potential_return and b.stake:
                    cumulative += b.potential_return - b.stake
                elif b.status == "lost" and b.stake:
                    cumulative -= b.stake
                elif b.status == "cashed_out" and b.cashed_out_amount and b.stake:
                    cumulative += b.cashed_out_amount - b.stake
            if cumulative > peak:
                peak = cumulative
            if peak - cumulative > drawdown:
                drawdown = peak - cumulative
        return round(drawdown, 2)
    drawdown = calculate_drawdown(bets)
    
    def calculate_sharpe_ratio(bets_list):
        returns = []
        for b in sorted(bets_list, key=lambda x: x.placed_at or datetime.min):
            if b.is_freebet:
                if b.status == "won" and b.potential_return and b.stake:
                    returns.append(b.potential_return / b.stake)
                elif b.status == "cashed_out" and b.cashed_out_amount and b.stake:
                    returns.append(b.cashed_out_amount / b.stake)
            else:
                if b.status == "won" and b.potential_return and b.stake:
                    returns.append((b.potential_return - b.stake) / b.stake)
                elif b.status == "lost" and b.stake:
                    returns.append(-1)
                elif b.status == "cashed_out" and b.cashed_out_amount and b.stake:
                    returns.append((b.cashed_out_amount - b.stake) / b.stake)
                else:
                    continue
        if not returns:
            return 0
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        std = variance ** 0.5
        return round(mean_return / std, 2) if std > 0 else 0
    sharpe_ratio = calculate_sharpe_ratio(bets)
    
    def calculate_best_streak(bets):
        best_streak = 0
        current_streak = 0
        worst_streak = 0
        current_worst = 0
        for b in sorted(bets, key=lambda x: x.placed_at or datetime.min):
            if b.status == "won":
                current_streak += 1
                current_worst = 0
                if current_streak > best_streak:
                    best_streak = current_streak
            elif b.status == "lost":
                current_worst += 1
                current_streak = 0
                if current_worst > worst_streak:
                    worst_streak = current_worst
        return best_streak, worst_streak
    best_streak, worst_streak = calculate_best_streak(bets)
    
    odds_win_rate = {
        '1.0-1.5': {'total': 0, 'won': 0},
        '1.5-2.0': {'total': 0, 'won': 0},
        '2.0-2.5': {'total': 0, 'won': 0},
        '2.5-3.0': {'total': 0, 'won': 0},
        '3.0-4.0': {'total': 0, 'won': 0},
        '4.0+': {'total': 0, 'won': 0}
    }
    for b in bets:
        if b.total_odds:
            range_key = get_odds_range(b.total_odds)
            if range_key in odds_win_rate:
                odds_win_rate[range_key]['total'] += 1
                if b.status == 'won':
                    odds_win_rate[range_key]['won'] += 1
    
    def calculate_best_performance_period(bets):
        if not bets:
            return "N/A", 0
        weekly_profit = {}
        for b in bets:
            if b.placed_at:
                week_key = b.placed_at.strftime("%Y-W%W")
                weekly_profit[week_key] = weekly_profit.get(week_key, 0) + bet_profit(b)
        if not weekly_profit:
            return "N/A", 0
        best_week = max(weekly_profit.items(), key=lambda x: x[1])
        return best_week[0], round(best_week[1], 2)
    best_week, best_week_profit = calculate_best_performance_period(bets)
    
    def calculate_most_profitable_sport(bets):
        sport_profit = {}
        for b in bets:
            key = b.sport or "Desconhecido"
            sport_profit[key] = sport_profit.get(key, 0) + bet_profit(b)
        if not sport_profit:
            return "N/A", 0
        best = max(sport_profit.items(), key=lambda x: x[1])
        return best[0], round(best[1], 2)
    best_sport, best_sport_profit = calculate_most_profitable_sport(bets)
    
    def calculate_best_bookmaker(bets):
        book_profit = {}
        for b in bets:
            key = b.bookmaker_obj.name if b.bookmaker_obj else (b.bookmaker or "Desconhecido")
            book_profit[key] = book_profit.get(key, 0) + bet_profit(b)
        if not book_profit:
            return "N/A", 0
        best = max(book_profit.items(), key=lambda x: x[1])
        return best[0], round(best[1], 2)
    best_bookmaker, best_bookmaker_profit = calculate_best_bookmaker(bets)
    
    sport_stats = {}
    for b in bets:
        key = b.sport or "Desconhecido"
        sport_stats.setdefault(key, {"staked": 0.0, "profit": 0.0, "count": 0})
        sport_stats[key]["staked"] += b.stake or 0.0
        sport_stats[key]["profit"] += bet_profit(b)
        sport_stats[key]["count"] += 1
    
    book_stats = {}
    for b in bets:
        key = b.bookmaker_obj.name if b.bookmaker_obj else (b.bookmaker or "Desconhecido")
        book_stats.setdefault(key, {"staked": 0.0, "profit": 0.0, "count": 0})
        book_stats[key]["staked"] += b.stake or 0.0
        book_stats[key]["profit"] += bet_profit(b)
        book_stats[key]["count"] += 1
    
    bankroll_stats = {}
    for b in bets:
        key = b.bankroll.name if b.bankroll else "Sem Banca"
        bankroll_stats.setdefault(key, {"staked": 0.0, "profit": 0.0, "count": 0, "won": 0, "lost": 0})
        bankroll_stats[key]["staked"] += b.stake or 0.0
        bankroll_stats[key]["profit"] += bet_profit(b)
        bankroll_stats[key]["count"] += 1
        if b.status == "won":
            bankroll_stats[key]["won"] += 1
        elif b.status == "lost":
            bankroll_stats[key]["lost"] += 1
    
    status_stats = {
        "open": {"count": 0, "staked": 0.0},
        "won": {"count": 0, "staked": 0.0, "profit": 0.0},
        "lost": {"count": 0, "staked": 0.0, "profit": 0.0},
        "void": {"count": 0, "staked": 0.0},
        "cashed_out": {"count": 0, "staked": 0.0, "profit": 0.0}
    }
    for b in bets:
        status = b.status or "open"
        if status in status_stats:
            status_stats[status]["count"] += 1
            status_stats[status]["staked"] += b.stake or 0.0
            if status in ["won", "lost", "cashed_out"]:
                status_stats[status]["profit"] += bet_profit(b)
    
    all_bankrolls = Bankroll.query.filter_by(user_id=current_user.id).all()
    all_sports = [s[0] for s in db.session.query(Bet.sport).filter_by(user_id=current_user.id).distinct().all() if s[0] is not None]
    sport_roi = {}
    for sport, data in sport_stats.items():
        sport_roi[sport] = (data["profit"] / data["staked"] * 100) if data["staked"] > 0 else 0.0
    
    current_balances = {}
    for roll in all_bankrolls:
        net = sum(tx.amount if tx.type == "deposit" else -tx.amount for tx in roll.transactions)
        current_balances[roll.id] = round(roll.starting_balance + net, 2)
    total_balance = sum(current_balances.values())
    
    odds_distribution = {"1.0-1.5": 0, "1.5-2.0": 0, "2.0-2.5": 0, "2.5-3.0": 0, "3.0-4.0": 0, "4.0+": 0}
    for b in bets:
        if b.total_odds:
            if b.total_odds < 1.5:
                odds_distribution["1.0-1.5"] += 1
            elif b.total_odds < 2.0:
                odds_distribution["1.5-2.0"] += 1
            elif b.total_odds < 2.5:
                odds_distribution["2.0-2.5"] += 1
            elif b.total_odds < 3.0:
                odds_distribution["2.5-3.0"] += 1
            elif b.total_odds < 4.0:
                odds_distribution["3.0-4.0"] += 1
            else:
                odds_distribution["4.0+"] += 1
    
    return render_template(
        "stats.html",
        bets=all_recent_bets,
        total_staked=round(total_staked, 2),
        total_profit=round(total_profit, 2),
        roi=round(roi, 2),
        win_rate=round(win_rate, 1),
        avg_odds=round(avg_odds, 2),
        total_bets_count=total_bets_count,
        drawdown=drawdown,
        sharpe_ratio=sharpe_ratio,
        best_streak=best_streak,
        worst_streak=worst_streak,
        best_week=best_week,
        best_week_profit=best_week_profit,
        best_sport=best_sport,
        best_sport_profit=best_sport_profit,
        best_bookmaker=best_bookmaker,
        best_bookmaker_profit=best_bookmaker_profit,
        odds_win_rate=odds_win_rate,
        sport_stats=sport_stats,
        sport_roi=sport_roi,
        book_stats=book_stats,
        bankroll_stats=bankroll_stats,
        status_stats=status_stats,
        all_bankrolls=all_bankrolls,
        all_sports=all_sports,
        selected_period=period,
        selected_bankroll=selected_bankroll,
        selected_sport=selected_sport,
        chart_labels=json.dumps(chart_labels),
        chart_data=json.dumps(chart_data),
        total_balance=round(total_balance, 2),
        current_balances=current_balances,
        odds_distribution=json.dumps(odds_distribution),
    )

# ===== TIPS =====
@app.route("/tips")
@login_required
def tips_feed():
    tips = Tip.query.filter(
        Tip.is_public == True,
        Tip.user_id != current_user.id
    ).order_by(Tip.created_at.desc()).all()
    return render_template("tips_feed.html", tips=tips)

@app.route("/tips/my")
@login_required
def my_tips():
    tips = Tip.query.filter_by(user_id=current_user.id).order_by(Tip.created_at.desc()).all()
    return render_template("my_tips.html", tips=tips)

@app.route("/tips/create/<int:bet_id>", methods=["GET", "POST"])
@login_required
def create_tip(bet_id):
    bet = Bet.query.filter_by(id=bet_id, user_id=current_user.id).first_or_404()
    if request.method == "POST":
        title = request.form.get("title")
        description = request.form.get("description")
        is_public = request.form.get("is_public") == 'on'
        if not title:
            flash("Title is required", "error")
            return render_template("create_tip.html", bet=bet)
        tip = Tip(
            user_id=current_user.id,
            bet_id=bet.id,
            title=title,
            description=description,
            is_public=is_public
        )
        db.session.add(tip)
        db.session.commit()
        flash("Tip created successfully!", "success")
        return redirect(url_for("my_tips"))
    return render_template("create_tip.html", bet=bet)

@app.route("/tips/<int:tip_id>/like", methods=["POST"])
@login_required
def like_tip(tip_id):
    tip = Tip.query.get_or_404(tip_id)
    tip.likes += 1
    db.session.commit()
    return jsonify({'likes': tip.likes})

@app.route("/tips/<int:tip_id>/delete", methods=["POST"])
@login_required
def delete_tip(tip_id):
    tip = Tip.query.filter_by(id=tip_id, user_id=current_user.id).first_or_404()
    db.session.delete(tip)
    db.session.commit()
    flash("Tip deleted successfully!", "success")
    return redirect(url_for("my_tips"))

@app.route("/tips/<int:tip_id>")
@login_required
def view_tip(tip_id):
    tip = Tip.query.get_or_404(tip_id)
    tip.views += 1
    db.session.commit()
    comments = TipComment.query.filter_by(tip_id=tip.id).order_by(TipComment.created_at.desc()).all()
    return render_template("tip_detail.html", tip=tip, comments=comments)

@app.route("/tips/<int:tip_id>/comment", methods=["POST"])
@login_required
def add_comment(tip_id):
    tip = Tip.query.get_or_404(tip_id)
    content = request.form.get("content")
    if not content:
        flash("Comment cannot be empty", "error")
        return redirect(url_for("view_tip", tip_id=tip.id))
    comment = TipComment(
        tip_id=tip.id,
        user_id=current_user.id,
        content=content
    )
    db.session.add(comment)
    db.session.commit()
    flash("Comment added!", "success")
    return redirect(url_for("view_tip", tip_id=tip.id))

# ===== VALUE BETS =====
@app.route("/value_bets")
@login_required
def value_bets():
    from value_detector import ValueBetDetector
    bets = Bet.query.filter_by(user_id=current_user.id).all()
    use_api = os.environ.get('ODDS_API_KEY') is not None
    detector = ValueBetDetector(bets, use_api=use_api)
    open_bets = [b for b in bets if b.status == 'open']
    value_bets = []
    for bet in open_bets:
        result = detector.detect_value(bet)
        if result:
            value_bets.append({
                'bet': bet,
                'value': result
            })
    value_bets.sort(key=lambda x: x['value']['value_pct'], reverse=True)
    stats = detector.get_statistics()
    return render_template(
        "value_bets.html",
        value_bets=value_bets,
        stats=stats,
        use_api=use_api
    )

@app.route("/api/value_bets")
@login_required
def api_value_bets():
    from value_detector import ValueBetDetector
    bets = Bet.query.filter_by(user_id=current_user.id).all()
    use_api = os.environ.get('ODDS_API_KEY') is not None
    detector = ValueBetDetector(bets, use_api=use_api)
    value_bets = detector.get_best_value_bets(limit=20)
    result = []
    for item in value_bets:
        bet = item['bet']
        value = item['value']
        result.append({
            'id': bet.id,
            'sport': bet.sport,
            'market_type': bet.market_type,
            'total_odds': bet.total_odds,
            'stake': bet.stake,
            'value_pct': value['value_pct'],
            'real_prob': value['real_prob'],
            'implied_prob': value['implied_prob'],
            'expected_value': value['expected_value'],
            'recommendation': value['recommendation'],
            'confidence': value['confidence'],
            'source': value.get('source', 'Historical')
        })
    return jsonify({'value_bets': result, 'count': len(result)})

@app.route("/value_bets/simulate/<int:bet_id>", methods=["POST"])
@login_required
def simulate_value_bet(bet_id):
    bet = Bet.query.filter_by(id=bet_id, user_id=current_user.id).first_or_404()
    from value_detector import ValueBetDetector
    import random
    bets = Bet.query.filter_by(user_id=current_user.id).all()
    use_api = os.environ.get('ODDS_API_KEY') is not None
    detector = ValueBetDetector(bets, use_api=use_api)
    result = detector.detect_value(bet)
    if not result:
        return jsonify({'error': 'No value detected'}), 400
    simulations = []
    for i in range(100):
        if random.random() < (result['real_prob'] / 100):
            simulations.append('win')
        else:
            simulations.append('loss')
    wins = simulations.count('win')
    losses = simulations.count('loss')
    expected_profit = 0
    if bet.stake:
        expected_profit = (wins * bet.stake * (bet.total_odds - 1)) - (losses * bet.stake)
    return jsonify({
        'simulations': 100,
        'wins': wins,
        'losses': losses,
        'win_rate_sim': round(wins / 100 * 100, 1),
        'expected_profit': round(expected_profit, 2)
    })

# ===== EXPORT =====
import csv
import io
from flask import send_file, make_response

@app.route("/export/bets")
@login_required
def export_bets():
    # Obter o bankroll_id da query string
    bankroll_id = request.args.get('bankroll_id', '')
    
    # Base da query
    query = Bet.query.filter_by(user_id=current_user.id)
    
    # Filtrar por bankroll se especificado
    bankroll_name = None
    if bankroll_id and bankroll_id.isdigit():
        bankroll = Bankroll.query.filter_by(id=int(bankroll_id), user_id=current_user.id).first()
        if bankroll:
            query = query.filter_by(bankroll_id=int(bankroll_id))
            bankroll_name = bankroll.name
    
    bets = query.order_by(Bet.placed_at.desc()).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Cabeçalhos
    writer.writerow([
        "ID", 
        "Bookmaker", 
        "Bankroll ID",
        "Bankroll Name",
        "Sport", 
        "Market Type", 
        "Total Odds", 
        "Stake", 
        "Potential Return", 
        "Currency", 
        "Status", 
        "Placed At", 
        "Notes"
    ])
    
    # Dados
    for bet in bets:
        writer.writerow([
            bet.id,
            bet.bookmaker_obj.name if bet.bookmaker_obj else bet.bookmaker,
            bet.bankroll.id if bet.bankroll else "",
            bet.bankroll.name if bet.bankroll else "",
            bet.sport,
            bet.market_type,
            bet.total_odds,
            bet.stake,
            bet.potential_return,
            bet.currency,
            bet.status,
            bet.placed_at.strftime("%Y-%m-%d %H:%M:%S") if bet.placed_at else "",
            bet.notes
        ])
    
    output.seek(0)
    
    # Nome do ficheiro com bankroll
    filename = f'bets_export'
    if bankroll_name:
        filename += f'_{bankroll_name.replace(" ", "_")}'
    filename += f'_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.csv'
    
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=filename
    )

@app.route("/export/bankrolls")
@login_required
def export_bankrolls():
    bankrolls = Bankroll.query.filter_by(user_id=current_user.id).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Bankroll ID", "Bankroll Name", "Currency", "Starting Balance", "Transaction ID", "Transaction Type", "Amount", "Bookmaker", "Notes", "Transaction Date"])
    for roll in bankrolls:
        if roll.transactions:
            for tx in roll.transactions:
                writer.writerow([
                    roll.id,
                    roll.name,
                    roll.currency,
                    roll.starting_balance,
                    tx.id,
                    tx.type,
                    tx.amount,
                    tx.bookmaker_obj.name if tx.bookmaker_obj else "",
                    tx.notes,
                    tx.created_at.strftime("%Y-%m-%d %H:%M:%S")
                ])
        else:
            writer.writerow([roll.id, roll.name, roll.currency, roll.starting_balance, "", "", "", "", "", ""])
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'bankrolls_export_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.csv'
    )

@app.route("/export/bookmakers")
@login_required
def export_bookmakers():
    bookmakers = Bookmaker.query.filter_by(user_id=current_user.id).order_by(Bookmaker.name.asc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Currency", "Starting Balance", "Created At"])
    for book in bookmakers:
        writer.writerow([
            book.id,
            book.name,
            book.currency,
            book.starting_balance,
            book.created_at.strftime("%Y-%m-%d %H:%M:%S") if book.created_at else ""
        ])
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'bookmakers_export_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.csv'
    )

@app.route("/export/all")
@login_required
def export_all():
    """Página de exportação com opções de bankroll"""
    # Obter todos os bankrolls do utilizador
    bankrolls = Bankroll.query.filter_by(user_id=current_user.id).order_by(Bankroll.name.asc()).all()
    
    # Obter estatísticas de bets por bankroll
    bankroll_stats = {}
    for b in bankrolls:
        count = Bet.query.filter_by(user_id=current_user.id, bankroll_id=b.id).count()
        bankroll_stats[b.id] = count
    
    # Total de bets
    total_bets = Bet.query.filter_by(user_id=current_user.id).count()
    
    return render_template(
        "export.html",
        bankrolls=bankrolls,
        bankroll_stats=bankroll_stats,
        total_bets=total_bets
    )

# ===== IMPORT =====
@app.route("/import", methods=["GET", "POST"])
@login_required
def import_data():
    bankrolls = Bankroll.query.filter_by(user_id=current_user.id).order_by(Bankroll.name.asc()).all()
    
    if request.method == "POST":
        file = request.files.get("file")
        import_type = request.form.get("import_type")
        
        if not file or file.filename == "":
            flash("No file uploaded", "error")
            return redirect(request.url)
        
        if not import_type:
            flash("Please select the type of data to import", "error")
            return redirect(request.url)
        
        try:
            content = file.read().decode('utf-8-sig')
            lines = content.splitlines()
            
            if import_type == "bookmakers":
                imported = import_bookmakers(lines, current_user.id)
                flash(f"✅ {imported} bookmakers imported successfully!", "success")
            elif import_type == "bankrolls":
                imported = import_bankrolls(lines, current_user.id)
                flash(f"✅ {imported} bankrolls imported successfully!", "success")
            elif import_type == "bets":
                # Obter o bankroll_id para as bets
                bankroll_id = request.form.get("bankroll_id")
                if not bankroll_id:
                    flash("Please select a bankroll for the bets", "error")
                    return redirect(request.url)
                imported = import_bets(lines, current_user.id, int(bankroll_id))
                flash(f"✅ {imported} bets imported successfully!", "success")
            else:
                flash("Invalid import type", "error")
            
            return redirect(url_for("import_data"))
            
        except Exception as e:
            flash(f"Error importing: {str(e)}", "error")
            return redirect(request.url)
    
    return render_template("import_data.html", bankrolls=bankrolls)

def import_bookmakers(lines, user_id):
    """Importa bookmakers a partir de um CSV"""
    imported = 0
    reader = csv.DictReader(lines)
    
    for row in reader:
        name = row.get('Name', '').strip()
        currency = row.get('Currency', 'EUR').strip()
        starting_balance = float(row.get('Starting Balance', 0) or 0)
        
        if not name:
            continue
        
        # Verificar se já existe
        existing = Bookmaker.query.filter_by(name=name, user_id=user_id).first()
        if existing:
            # Atualizar
            existing.currency = currency
            existing.starting_balance = starting_balance
        else:
            # Criar
            bookmaker = Bookmaker(
                name=name,
                currency=currency,
                starting_balance=starting_balance,
                user_id=user_id
            )
            db.session.add(bookmaker)
        
        imported += 1
    
    db.session.commit()
    return imported

def import_bankrolls(lines, user_id):
    """Importa bankrolls a partir de um CSV"""
    imported = 0
    reader = csv.DictReader(lines)
    
    bankrolls_data = {}
    
    for row in reader:
        bankroll_id = int(row.get('Bankroll ID', 0))
        name = row.get('Bankroll Name', '').strip()
        currency = row.get('Currency', 'EUR').strip()
        starting_balance = float(row.get('Starting Balance', 0) or 0)
        
        if not name:
            continue
        
        # Verificar se o bankroll já existe
        existing = Bankroll.query.filter_by(name=name, user_id=user_id).first()
        
        if existing:
            bankroll = existing
        else:
            bankroll = Bankroll(
                name=name,
                currency=currency,
                starting_balance=starting_balance,
                user_id=user_id
            )
            db.session.add(bankroll)
            db.session.flush()
            imported += 1
        
        # Processar transações se existirem
        tx_type = row.get('Transaction Type', '').strip()
        tx_amount = row.get('Amount', '').strip()
        
        if tx_type and tx_amount:
            amount = float(tx_amount or 0)
            if amount > 0:
                bookmaker_name = row.get('Bookmaker', '').strip()
                bookmaker = None
                if bookmaker_name:
                    bookmaker = Bookmaker.query.filter_by(name=bookmaker_name, user_id=user_id).first()
                
                tx = Transaction(
                    bankroll_id=bankroll.id,
                    type=tx_type,
                    amount=amount,
                    notes=row.get('Notes', ''),
                    bookmaker_id=bookmaker.id if bookmaker else None
                )
                db.session.add(tx)
    
    db.session.commit()
    return imported

def import_bets(lines, user_id, bankroll_id):
    """Importa bets a partir de um CSV para um bankroll específico"""
    imported = 0
    reader = csv.DictReader(lines)
    
    for row in reader:
        try:
            # Obter bookmaker
            bookmaker_name = row.get('Bookmaker', '').strip()
            bookmaker_id = None
            if bookmaker_name:
                bookmaker = Bookmaker.query.filter_by(name=bookmaker_name, user_id=user_id).first()
                if bookmaker:
                    bookmaker_id = bookmaker.id
                else:
                    # Criar bookmaker se não existir
                    new_book = Bookmaker(name=bookmaker_name, user_id=user_id)
                    db.session.add(new_book)
                    db.session.flush()
                    bookmaker_id = new_book.id
            
            # Parse da data
            placed_at = None
            date_str = row.get('Placed At', '').strip()
            if date_str:
                try:
                    placed_at = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    placed_at = datetime.utcnow()
            else:
                placed_at = datetime.utcnow()
            
            # Parse do stake
            stake = None
            stake_str = row.get('Stake', '').strip()
            if stake_str:
                try:
                    stake = float(stake_str)
                except ValueError:
                    stake = None
            
            # Parse das odds
            total_odds = None
            odds_str = row.get('Total Odds', '').strip()
            if odds_str:
                try:
                    total_odds = float(odds_str)
                except ValueError:
                    total_odds = None
            
            # Parse do potencial retorno
            potential_return = None
            return_str = row.get('Potential Return', '').strip()
            if return_str:
                try:
                    potential_return = float(return_str)
                except ValueError:
                    potential_return = None
            
            # Status
            status = row.get('Status', 'open').strip().lower()
            
            # Criar bet
            bet = Bet(
                bookmaker=bookmaker_name,
                bookmaker_id=bookmaker_id,
                bankroll_id=bankroll_id,  # <-- Associado ao bankroll selecionado
                sport=row.get('Sport', '').strip(),
                market_type=row.get('Market Type', '').strip(),
                total_odds=total_odds,
                stake=stake,
                potential_return=potential_return,
                currency=row.get('Currency', 'EUR').strip(),
                status=status,
                placed_at=placed_at,
                notes=row.get('Notes', ''),
                user_id=user_id
            )
            db.session.add(bet)
            imported += 1
            
        except Exception as e:
            print(f"Error importing bet: {e}")
            continue
    
    db.session.commit()
    return imported

def parse_bet_row(parts):
    return {
        'bet_number': parts[0].strip() if len(parts) > 0 else None,
        'date': parts[1].strip() if len(parts) > 1 else None,
        'type': parts[2].strip() if len(parts) > 2 else None,
        'event': parts[3].strip() if len(parts) > 3 else None,
        'selection': parts[4].strip() if len(parts) > 4 else None,
        'market': parts[5].strip() if len(parts) > 5 else None,
        'sport': parts[6].strip() if len(parts) > 6 else None,
        'bookmaker': parts[7].strip() if len(parts) > 7 else None,
        'odds': parts[8].strip() if len(parts) > 8 else None,
        'stake': parts[9].strip() if len(parts) > 9 else None,
        'result': parts[10].strip() if len(parts) > 10 else None,
        'profit': parts[11].strip() if len(parts) > 11 else None,
    }

def create_leg_from_row(parts, offset):
    leg = {}
    if len(parts) >= 6:
        leg['event'] = parts[0].strip() if parts[0].strip() else None
        leg['selection'] = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        leg['market'] = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        leg['sport'] = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None
        leg['bookmaker'] = parts[4].strip() if len(parts) > 4 and parts[4].strip() else None
        leg['result'] = parts[5].strip() if len(parts) > 5 and parts[5].strip() else None
        if len(parts) > 6 and parts[6].strip():
            try:
                leg['odds'] = float(parts[6].strip().replace(',', '.'))
            except ValueError:
                leg['odds'] = None
        else:
            leg['odds'] = None
        if leg['event'] or leg['selection'] or leg['market']:
            return leg
    return None

def save_bet_with_legs(bet_data, legs_data, bankroll_id):
    from datetime import datetime
    bet_type = bet_data.get('type', 'Simple')
    placed_at = None
    if bet_data.get('date'):
        try:
            placed_at = datetime.strptime(bet_data['date'], '%d/%m/%Y')
        except ValueError:
            placed_at = datetime.utcnow()
    stake = None
    if bet_data.get('stake'):
        try:
            stake = float(bet_data['stake'].replace(' €', '').replace(',', '.').strip())
        except ValueError:
            stake = None
    total_odds = None
    if bet_data.get('odds'):
        try:
            total_odds = float(bet_data['odds'].replace(',', '.').strip())
        except ValueError:
            total_odds = None
    status = 'open'
    result = bet_data.get('result', '').lower()
    if result == 'won':
        status = 'won'
    elif result == 'lost':
        status = 'lost'
    elif result == 'void':
        status = 'void'
    elif result == 'cashout':
        status = 'cashed_out'
    bookmaker_id = None
    bookmaker_name = bet_data.get('bookmaker', '').strip()
    if bookmaker_name:
        existing = Bookmaker.query.filter(Bookmaker.name.ilike(bookmaker_name), Bookmaker.user_id == current_user.id).first()
        if existing:
            bookmaker_id = existing.id
        else:
            new_book = Bookmaker(name=bookmaker_name, user_id=current_user.id)
            db.session.add(new_book)
            db.session.flush()
            bookmaker_id = new_book.id
    potential_return = None
    if stake and total_odds:
        potential_return = stake * total_odds
    bet = Bet(
        id=get_next_bet_id(),
        bookmaker=bookmaker_name,
        bookmaker_id=bookmaker_id,
        bankroll_id=bankroll_id,
        sport=bet_data.get('sport'),
        market_type=bet_data.get('type'),
        total_odds=total_odds,
        stake=stake,
        potential_return=potential_return,
        currency='EUR',
        status=status,
        placed_at=placed_at or datetime.utcnow(),
        notes=f"Imported from CSV - Bet #{bet_data.get('bet_number')}",
        user_id=current_user.id
    )
    db.session.add(bet)
    db.session.flush()
    for leg_data in legs_data:
        leg_status = 'pending'
        leg_result = leg_data.get('result', '').lower()
        if leg_result == 'won':
            leg_status = 'won'
        elif leg_result == 'lost':
            leg_status = 'lost'
        elif leg_result == 'void':
            leg_status = 'void'
        leg = BetLeg(
            bet_id=bet.id,
            event=leg_data.get('event'),
            team=leg_data.get('selection'),
            market=leg_data.get('market'),
            odds_decimal=leg_data.get('odds'),
            status=leg_status,
            is_builder='Builder' in (bet_data.get('type') or '')
        )
        db.session.add(leg)
    if status in ('won', 'lost', 'cashed_out') and bankroll_id and bookmaker_id and stake:
        db.session.add(Transaction(
            bankroll_id=bankroll_id,
            bookmaker_id=bookmaker_id,
            bet_id=bet.id,
            type='withdrawal',
            amount=stake,
            notes=f"Stake for bet #{bet.id}"
        ))
        if status == 'won' and potential_return:
            db.session.add(Transaction(
                bankroll_id=bankroll_id,
                bookmaker_id=bookmaker_id,
                bet_id=bet.id,
                type='deposit',
                amount=potential_return,
                notes=f"Payout for bet #{bet.id}"
            ))

# ===== ADMIN =====
@app.route("/admin")
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        flash("Access denied", "error")
        return redirect(url_for("index"))
    total_users = User.query.count()
    total_bets = Bet.query.count()
    total_tips = Tip.query.count()
    total_bankrolls = Bankroll.query.count()
    total_logs = UserLog.query.count()
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    active_users = User.query.filter(User.created_at >= thirty_days_ago).count()
    recent_logs = UserLog.query.order_by(UserLog.created_at.desc()).limit(50).all()
    action_stats = db.session.query(UserLog.action, db.func.count(UserLog.id)).group_by(UserLog.action).all()
    return render_template(
        "admin.html",
        total_users=total_users,
        total_bets=total_bets,
        total_tips=total_tips,
        total_bankrolls=total_bankrolls,
        total_logs=total_logs,
        active_users=active_users,
        recent_logs=recent_logs,
        action_stats=action_stats
    )

@app.route("/admin/logs")
@login_required
def admin_logs():
    if not current_user.is_admin:
        flash("Access denied", "error")
        return redirect(url_for("index"))
    action_filter = request.args.get('action', '')
    user_filter = request.args.get('user', '')
    query = UserLog.query
    if action_filter:
        query = query.filter(UserLog.action == action_filter)
    if user_filter and user_filter.isdigit():
        query = query.filter(UserLog.user_id == int(user_filter))
    logs = query.order_by(UserLog.created_at.desc()).paginate(page=1, per_page=50)
    actions = db.session.query(UserLog.action).distinct().all()
    users = User.query.all()
    return render_template(
        "admin_logs.html",
        logs=logs,
        actions=actions,
        users=users,
        action_filter=action_filter,
        user_filter=user_filter
    )

@app.route("/admin/logs/download")
@login_required
def download_logs():
    if not current_user.is_admin:
        flash("Access denied", "error")
        return redirect(url_for("index"))
    logs = UserLog.query.order_by(UserLog.created_at.desc()).all()
    output = io.StringIO()
    output.write("Time,User,Action,Details,IP\n")
    for log in logs:
        output.write(f"{log.created_at},{log.user.username if log.user else 'Anonymous'},{log.action},{log.details or ''},{log.ip_address or ''}\n")
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'logs_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.csv'
    )

@app.route("/admin/logs/clear", methods=["POST"])
@login_required
def clear_logs():
    if not current_user.is_admin:
        flash("Access denied", "error")
        return redirect(url_for("index"))
    UserLog.query.delete()
    db.session.commit()
    app.logger.info(f"🧹 Logs cleared by admin: {current_user.username}")
    flash("Logs cleared successfully!", "success")
    return redirect(url_for("admin_dashboard"))

# ===== HEALTH CHECK =====
@app.route("/health")
def health_check():
    status = {
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'version': '1.0.0'
    }
    try:
        db.session.execute(db.text('SELECT 1'))
        status['database'] = 'connected'
    except Exception as e:
        status['database'] = f'error: {str(e)}'
        status['status'] = 'unhealthy'
    return jsonify(status)

# ===== ROTAS DE UPLOAD DE FICHEIROS =====
@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/bets/<int:bet_id>/delete_bulk", methods=["POST"])
@login_required
def delete_bets_bulk_route(bet_id):
    return delete_bets_bulk()

@app.route("/bankrolls/<int:roll_id>/bets")
@login_required
def bets_for_bankroll(roll_id):
    roll = Bankroll.query.filter_by(id=roll_id, user_id=current_user.id).first_or_404()
    bets = Bet.query.filter_by(bankroll_id=roll.id).order_by(Bet.placed_at.desc()).all()
    return render_template("bets.html", bets=bets, title=f"Bets for bankroll: {roll.name}")

@app.route("/bookmakers/<int:book_id>/bets")
@login_required
def bets_for_bookmaker(book_id):
    book = Bookmaker.query.filter_by(id=book_id, user_id=current_user.id).first_or_404()
    bets = Bet.query.filter_by(bookmaker_id=book.id).order_by(Bet.placed_at.desc()).all()
    return render_template("bets.html", bets=bets, title=f"Bets for bookmaker: {book.name}")

@app.route("/save_manual_bet", methods=["POST"])
@login_required
def save_manual_bet():
    bet_type = request.form.get('bet_type', 'single')
    status = request.form.get('status') or 'open'
    if status == 'cashout':
        status = 'cashed_out'
    bankroll_id = request.form.get('bankroll_id') or None
    notes = request.form.get('notes')
    is_freebet = request.form.get('is_freebet') == 'on'
    is_live = request.form.get('is_live') == 'on'
    placed_at_raw = request.form.get('event_date')
    try:
        placed_at = datetime.fromisoformat(placed_at_raw) if placed_at_raw else datetime.utcnow()
    except ValueError:
        placed_at = datetime.utcnow()

    def parse_float(name):
        raw = request.form.get(name)
        if not raw:
            return None
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            return None

    if bet_type == 'combined':
        bookmaker_id = request.form.get('bookmaker_id_combined') or None
        total_odds = parse_float('global_odds')
        stake = parse_float('combined_stake')
        sport = None
        market_type = "Combined"
        events = request.form.getlist('leg_event[]')
        selections = request.form.getlist('leg_selection[]')
        odds_raw = request.form.getlist('leg_odd[]')
        sports_legs = request.form.getlist('leg_sport[]')
    else:
        bookmaker_id = request.form.get('bookmaker_id') or None
        total_odds = parse_float('single_odds')
        stake = parse_float('single_stake')
        sport = request.form.get('sport')
        market_type = request.form.get('market')
        events = selections = odds_raw = sports_legs = []
    potential_return = round(stake * total_odds, 2) if (stake and total_odds) else None
    new_bet = Bet(
        id=get_next_bet_id(),
        sport=sport,
        market_type=market_type,
        bookmaker_id=int(bookmaker_id) if bookmaker_id else None,
        bankroll_id=int(bankroll_id) if bankroll_id else None,
        total_odds=total_odds,
        stake=stake,
        potential_return=potential_return,
        status=status,
        placed_at=placed_at,
        notes=notes,
        is_freebet=is_freebet,
        is_live=is_live,
        user_id=current_user.id
    )
    db.session.add(new_bet)
    db.session.flush()
    if bet_type == 'combined':
        for i, event in enumerate(events):
            leg_odds = None
            if i < len(odds_raw) and odds_raw[i]:
                try:
                    leg_odds = float(odds_raw[i].replace(",", "."))
                except ValueError:
                    leg_odds = None
            selection = selections[i] if i < len(selections) else None
            leg_sport = sports_legs[i] if i < len(sports_legs) else None
            if not event and not selection and leg_odds is None:
                continue
            db.session.add(BetLeg(
                bet_id=new_bet.id,
                event=event or None,
                team=selection or None,
                market=leg_sport or None,
                odds_decimal=leg_odds,
            ))
    db.session.commit()
    flash(f"Bet #{new_bet.id} saved.", "success")
    return redirect(url_for('bets_list'))

# ===== PUSH NOTIFICATIONS =====
@app.route("/api/subscribe", methods=["POST"])
@login_required
def subscribe_push():
    data = request.get_json()
    if not data or 'endpoint' not in data:
        return jsonify({'error': 'Invalid subscription'}), 400
    PushSubscription.query.filter_by(
        user_id=current_user.id,
        endpoint=data['endpoint']
    ).delete()
    sub = PushSubscription(
        user_id=current_user.id,
        endpoint=data['endpoint'],
        auth_key=data.get('keys', {}).get('auth'),
        p256dh_key=data.get('keys', {}).get('p256dh')
    )
    db.session.add(sub)
    db.session.commit()
    return jsonify({'status': 'success'})

@app.route("/api/unsubscribe", methods=["POST"])
@login_required
def unsubscribe_push():
    data = request.get_json()
    if not data or 'endpoint' not in data:
        return jsonify({'error': 'Invalid subscription'}), 400
    PushSubscription.query.filter_by(
        user_id=current_user.id,
        endpoint=data['endpoint']
    ).delete()
    db.session.commit()
    return jsonify({'status': 'success'})

# ===== CRIAR TABELAS E UTILIZADORES PADRÃO =====
with app.app_context():
    migrate_database()
    db.create_all()
    
    # ===== CRIAR UTILIZADORES APENAS A PARTIR DE VARIÁVEIS DE AMBIENTE =====
    # NUNCA usar emails ou passwords hardcoded no código!
    
    admin_username = os.environ.get('ADMIN_USERNAME', 'admin')
    admin_email = os.environ.get('ADMIN_EMAIL')
    admin_password = os.environ.get('ADMIN_PASSWORD')
    
    user_username = os.environ.get('USER_USERNAME', 'tiago32rodriguez')
    user_email = os.environ.get('USER_EMAIL')
    user_password = os.environ.get('USER_PASSWORD')
    
    # Criar admin (apenas se a password e email estiverem definidos)
    if admin_password and admin_email:
        admin = User.query.filter_by(username=admin_username).first()
        if not admin:
            admin = User(
                username=admin_username,
                email=admin_email,
                password_hash=generate_password_hash(admin_password),
                is_admin=True,
                is_active=True
            )
            db.session.add(admin)
            db.session.commit()
            print(f"✅ Admin criado: {admin_username}")
        else:
            print(f"✅ Admin já existe: {admin_username}")
    else:
        print("⚠️ ADMIN_PASSWORD ou ADMIN_EMAIL não definida. Admin não criado.")
    
    # Criar utilizador normal (apenas se a password e email estiverem definidos)
    if user_password and user_email:
        user = User.query.filter_by(username=user_username).first()
        if not user:
            user = User(
                username=user_username,
                email=user_email,
                password_hash=generate_password_hash(user_password),
                is_admin=True,
                is_active=True
            )
            db.session.add(user)
            db.session.commit()
            print(f"✅ Utilizador criado: {user_username}")
        else:
            print(f"✅ Utilizador já existe: {user_username}")
    else:
        print(f"⚠️ USER_PASSWORD ou USER_EMAIL não definida. Utilizador {user_username} não criado.")
    
    # ===== IMPORTAR UTILIZADOR DO JSON (se existir) =====
    import json
    import os
    
    if os.path.exists('my_user.json'):
        try:
            with open('my_user.json', 'r') as f:
                user_data = json.load(f)
            
            print(f"📥 Importando utilizador do JSON: {user_data['username']}")
            
            user = User.query.filter_by(username=user_data['username']).first()
            if user:
                print(f"⚠️ Utilizador {user_data['username']} já existe. A atualizar...")
                user.email = user_data['email']
                user.password_hash = user_data['password_hash']
                user.is_admin = user_data['is_admin']
                user.is_active = user_data['is_active']
                db.session.commit()
                print(f"✅ Utilizador {user_data['username']} atualizado!")
            else:
                user = User(
                    username=user_data['username'],
                    email=user_data['email'],
                    password_hash=user_data['password_hash'],
                    is_admin=user_data['is_admin'],
                    is_active=user_data['is_active']
                )
                db.session.add(user)
                db.session.commit()
                print(f"✅ Utilizador {user_data['username']} importado com sucesso!")
            
            # Apagar o ficheiro após importar (segurança)
            # os.remove('my_user.json')
            # print("🗑️ Ficheiro my_user.json removido")
            
        except Exception as e:
            print(f"⚠️ Erro ao importar utilizador do JSON: {e}")
    else:
        print("ℹ️ Ficheiro my_user.json não encontrado.")
    
    # ===== ASSOCIAR DADOS AO ADMIN =====
    try:
        admin = User.query.filter_by(username='admin').first()
        if admin:
            bankrolls = Bankroll.query.filter(Bankroll.user_id.is_(None)).all()
            for b in bankrolls:
                b.user_id = admin.id
            bookmakers = Bookmaker.query.filter(Bookmaker.user_id.is_(None)).all()
            for b in bookmakers:
                b.user_id = admin.id
            bets = Bet.query.filter(Bet.user_id.is_(None)).all()
            for b in bets:
                b.user_id = admin.id
            db.session.commit()
            if bankrolls or bookmakers or bets:
                print(f"✅ Associados {len(bankrolls)} bankrolls, {len(bookmakers)} bookmakers, {len(bets)} bets ao admin")
    except Exception as e:
        print(f"⚠️ Erro ao associar dados ao admin: {e}")
        db.session.rollback()
    
    # Se houver apenas um bankroll, definir como ativo
    admin_user = User.query.filter_by(username='admin').first()
    if admin_user:
        bankrolls = Bankroll.query.filter_by(user_id=admin_user.id).all()
        if len(bankrolls) == 1:
            bankroll = bankrolls[0]
            if bankroll and not bankroll.is_active:
                bankroll.is_active = True
                db.session.commit()
                print(f"✅ Bankroll '{bankroll.name}' definido como ativo.")
    
    print(f"✅ Total de bankrolls: {Bankroll.query.count()}")
    print(f"✅ Total de bookmakers: {Bookmaker.query.count()}")
    print(f"✅ Total de bets: {Bet.query.count()}")
    
    # Listar utilizadores existentes
    print("\n📋 Utilizadores existentes:")
    for u in User.query.all():
        print(f"   - {u.username} (ID: {u.id})")

# ===== INICIALIZAÇÃO DA APP =====
if __name__ == "__main__":
    debug = os.environ.get('FLASK_DEBUG', 'False') == 'True'
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=debug)

    
                