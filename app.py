# app.py
import os
import base64
import time
import json
import requests
import logging
from logging.handlers import RotatingFileHandler, SMTPHandler
import traceback
import sys
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from functools import wraps
import csv
import io
from flask import send_file, make_response
from flask import request, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import re

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_from_directory,
)
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
import requests

from parser import parse_betslip_from_gemini

print("DEBUG: app.py loaded from", __file__)

# ====== CONFIG & ENV ======

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

load_dotenv(os.path.join(BASE_DIR, ".env"))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY not set in .env or environment variables")

# Gemini Flash endpoint
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-in-production"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "bets.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
# ===== CONFIGURAÇÕES DA APP =====
app.config['APP_NAME'] = 'BETWISE'
app.config['APP_TAGLINE'] = 'analytics · betting'
app.config['APP_LOGO'] = '📊'
app.config['APP_FAVICON'] = '📊'
app.config['APP_COLOR'] = '#00d4aa'

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'

# ====== MODELS ======

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
    
    # NOVOS CAMPOS
    is_freebet = db.Column(db.Boolean, default=False)  # Freebet flag
    is_live = db.Column(db.Boolean, default=False)     # Live bet flag
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    
    # cashout fields
    cashed_out_amount = db.Column(db.Float)
    cashed_out_at = db.Column(db.DateTime)

    # links
    bookmaker_id = db.Column(db.Integer, db.ForeignKey("bookmaker.id"))
    bankroll_id = db.Column(db.Integer, db.ForeignKey("bankroll.id"))
    bookmaker_obj = db.relationship("Bookmaker")
    bankroll = db.relationship("Bankroll")

    legs = db.relationship("BetLeg", backref="bet", cascade="all, delete-orphan")
    
# Modelo User
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

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))    


class BetLeg(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bet_id = db.Column(db.Integer, db.ForeignKey("bet.id"), nullable=False)
    # optional: event/match name; you can wire this from parser later
    event = db.Column(db.String(256))
    team = db.Column(db.String(128))
    market = db.Column(db.String(128))
    odds_decimal = db.Column(db.Float)  # may be NULL for bet builder legs
    status = db.Column(db.String(16), default="pending")  # pending/won/lost/void
    is_builder = db.Column(db.Boolean, default=False)
    
class Bankroll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True)
    currency = db.Column(db.String(8), default="EUR")
    starting_balance = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=False)  # NOVO CAMPO
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    
    transactions = db.relationship(
        "Transaction", backref="bankroll", cascade="all, delete-orphan"
    )

class Bookmaker(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True)
    starting_balance = db.Column(db.Float, default=0.0)  # Saldo inicial do bookmaker
    currency = db.Column(db.String(8), default="EUR")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    
class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bankroll_id = db.Column(db.Integer, db.ForeignKey("bankroll.id"), nullable=False)
    type = db.Column(db.String(16))  # "deposit" or "withdrawal" or "adjustment"
    amount = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    notes = db.Column(db.Text)   
    bookmaker_id = db.Column(db.Integer, db.ForeignKey("bookmaker.id"))
    bet_id = db.Column(db.Integer, db.ForeignKey("bet.id"))
    
    bookmaker_obj = db.relationship("Bookmaker")
    
# Novo modelo para guardar o balance por bookmaker em cada bankroll
class BankrollBookmakerBalance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bankroll_id = db.Column(db.Integer, db.ForeignKey("bankroll.id"), nullable=False)
    bookmaker_id = db.Column(db.Integer, db.ForeignKey("bookmaker.id"), nullable=False)
    starting_balance = db.Column(db.Float, default=0.0)
    current_balance = db.Column(db.Float, default=0.0)
    
    bankroll = db.relationship("Bankroll", backref="bookmaker_balances")
    bookmaker = db.relationship("Bookmaker")
    
    __table_args__ = (
        db.UniqueConstraint('bankroll_id', 'bookmaker_id', name='unique_bankroll_bookmaker'),
    )
    
    def can_withdraw(self, amount):
        """Verifica se é possível levantar este valor"""
        return self.current_balance >= amount
    
    def withdraw(self, amount):
        """Faz um levantamento (diminui o balance)"""
        if not self.can_withdraw(amount):
            raise ValueError(f"Insufficient balance. Current: {self.current_balance}, Attempted: {amount}")
        self.current_balance -= amount
        return self.current_balance
    
    def deposit(self, amount):
        """Faz um depósito (aumenta o balance)"""
        self.current_balance += amount
        return self.current_balance

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
        """Cria um novo log na base de dados"""
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
    
# ===== CONFIGURAÇÃO DE LOGGING =====
def setup_logging(app):
    """Configura o sistema de logging da aplicação"""
    
    # Remover handlers existentes
    for handler in app.logger.handlers[:]:
        app.logger.removeHandler(handler)
    
    # Nível de logging
    log_level = logging.DEBUG if app.debug else logging.INFO
    app.logger.setLevel(log_level)
    
    # Formato dos logs
    log_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 1. Handler para ficheiro (com rotação)
    if not os.path.exists('logs'):
        os.makedirs('logs')
    
    file_handler = RotatingFileHandler(
        'logs/metrikatips.log',
        maxBytes=10485760,  # 10MB
        backupCount=10
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(log_format)
    app.logger.addHandler(file_handler)
    
    # 2. Handler para console (debug)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if app.debug else logging.INFO)
    console_handler.setFormatter(log_format)
    app.logger.addHandler(console_handler)
    
    # 3. Handler para erros críticos (email) - apenas em produção
    if not app.debug and app.config.get('MAIL_SERVER'):
        mail_handler = SMTPHandler(
            mailhost=(app.config['MAIL_SERVER'], app.config.get('MAIL_PORT', 587)),
            fromaddr=app.config['MAIL_FROM'],
            toaddrs=[app.config['ADMIN_EMAIL']],
            subject='METRIKATIPS - Critical Error',
            credentials=(app.config['MAIL_USERNAME'], app.config['MAIL_PASSWORD']),
            secure=()
        )
        mail_handler.setLevel(logging.ERROR)
        mail_handler.setFormatter(log_format)
        app.logger.addHandler(mail_handler)
    
    app.logger.info('=' * 50)
    app.logger.info('🚀 METRIKATIPS iniciada')
    app.logger.info(f'📁 Ambiente: {"Produção" if not app.debug else "Desenvolvimento"}')
    app.logger.info('=' * 50)
    
    return app.logger

# Configurar logging antes de iniciar a app
logger = setup_logging(app)

def log_action(action_type):
    """Decorador para logging de ações dos utilizadores"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            start_time = time.time()
            
            # Executar a função
            try:
                result = f(*args, **kwargs)
                execution_time = time.time() - start_time
                
                # Log da ação
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
                # Log do erro
                app.logger.error(
                    f"ERROR: {action_type} | {user_info} | "
                    f"IP: {request.remote_addr} | "
                    f"Error: {str(e)}\n{traceback.format_exc()}"
                )
                raise
                
        return decorated_function
    return decorator
    
# app.py - Adicionar
def detect_value_bets():
    """Detecta apostas com valor (odds > probabilidade real)"""
    # Isto é simplificado - na prática usarias ML
    bets = Bet.query.filter_by(user_id=current_user.id, status='open').all()
    value_bets = []
    
    for bet in bets:
        # Probabilidade implícita = 1/odds
        implied_prob = 1 / bet.total_odds if bet.total_odds else 0
        
        # Probabilidade real estimada (simplificada)
        # Na prática, isto seria um modelo de ML
        real_prob = implied_prob * 1.15  # Exemplo: 15% de value
        
        if real_prob > implied_prob:
            value_bets.append({
                'bet': bet,
                'value': (real_prob - implied_prob) * 100,
                'real_prob': real_prob,
                'implied_prob': implied_prob
            })
    
    return value_bets

def kelly_criterion(odds, win_rate):
    """Calcula a stake ideal usando Kelly Criterion"""
    # b = odds - 1 (ganho líquido)
    # p = win_rate (probabilidade estimada)
    # q = 1 - p (probabilidade de perder)
    # f = (b*p - q) / b
    
    b = odds - 1
    p = win_rate
    q = 1 - p
    f = (b * p - q) / b
    
    return max(0, min(f, 0.25))  # Limitar a 25% do bankroll
    
def migrate_database():
    """Migrate database schema to add new columns"""
    import sqlite3
    from datetime import datetime
    
    db_path = os.path.join(BASE_DIR, "bets.db")
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # ==================== TABELA USER ====================
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
            
            # Criar utilizador admin padrão
            from werkzeug.security import generate_password_hash
            cursor.execute("""
                INSERT INTO user (username, email, password_hash, is_admin)
                VALUES (?, ?, ?, ?)
            """, ("admin", "admin@metrikatips.com", generate_password_hash("admin123"), 1))
            print("Default admin user created (username: admin, password: admin123)")
        
        # ==================== TABELA BANKROLL ====================
        cursor.execute("PRAGMA table_info(bankroll)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'user_id' not in columns:
            print("Adding user_id column to bankroll...")
            cursor.execute("ALTER TABLE bankroll ADD COLUMN user_id INTEGER REFERENCES user(id)")
            # Associar todos os bankrolls ao admin (user_id = 1)
            cursor.execute("UPDATE bankroll SET user_id = 1 WHERE user_id IS NULL")
            print("Column user_id added to bankroll.")
        
        # ==================== TABELA BOOKMAKER ====================
        cursor.execute("PRAGMA table_info(bookmaker)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'user_id' not in columns:
            print("Adding user_id column to bookmaker...")
            cursor.execute("ALTER TABLE bookmaker ADD COLUMN user_id INTEGER REFERENCES user(id)")
            cursor.execute("UPDATE bookmaker SET user_id = 1 WHERE user_id IS NULL")
            print("Column user_id added to bookmaker.")
        
        # ==================== TABELA BET ====================
        cursor.execute("PRAGMA table_info(bet)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'user_id' not in columns:
            print("Adding user_id column to bet...")
            cursor.execute("ALTER TABLE bet ADD COLUMN user_id INTEGER REFERENCES user(id)")
            cursor.execute("UPDATE bet SET user_id = 1 WHERE user_id IS NULL")
            print("Column user_id added to bet.")
        
        # ==================== TABELA TRANSACTION ====================
        # Verificar se a tabela transaction tem a coluna bet_id
        cursor.execute("PRAGMA table_info(transaction)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'bet_id' not in columns:
            print("Adding bet_id column to transaction...")
            cursor.execute("ALTER TABLE transaction ADD COLUMN bet_id INTEGER REFERENCES bet(id)")
            print("Column bet_id added to transaction.")
        
        # ==================== TABELA BANKROLL_BOOKMAKER_BALANCE ====================
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='bankroll_bookmaker_balance'
        """)
        if not cursor.fetchone():
            print("Creating table bankroll_bookmaker_balance...")
            cursor.execute("""
                CREATE TABLE bankroll_bookmaker_balance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bankroll_id INTEGER NOT NULL,
                    bookmaker_id INTEGER NOT NULL,
                    starting_balance FLOAT DEFAULT 0.0,
                    current_balance FLOAT DEFAULT 0.0,
                    FOREIGN KEY (bankroll_id) REFERENCES bankroll(id),
                    FOREIGN KEY (bookmaker_id) REFERENCES bookmaker(id),
                    UNIQUE(bankroll_id, bookmaker_id)
                )
            """)
            print("Table bankroll_bookmaker_balance created.")
        
        # ==================== TABELA BET_LEG (verificar se existe) ====================
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='bet_leg'
        """)
        if not cursor.fetchone():
            print("Creating table bet_leg...")
            cursor.execute("""
                CREATE TABLE bet_leg (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bet_id INTEGER NOT NULL,
                    event VARCHAR(256),
                    team VARCHAR(128),
                    market VARCHAR(128),
                    odds_decimal FLOAT,
                    status VARCHAR(16) DEFAULT 'pending',
                    is_builder BOOLEAN DEFAULT 0,
                    FOREIGN KEY (bet_id) REFERENCES bet(id)
                )
            """)
            print("Table bet_leg created.")
        
        conn.commit()
        conn.close()
        print("Database migration completed successfully!")
        
    except Exception as e:
        print(f"Error during migration: {e}")
        # Tentar com SQLAlchemy como fallback
        try:
            with app.app_context():
                db.create_all()
                print("Database migration completed via SQLAlchemy!")
        except Exception as e2:
            print(f"Error during SQLAlchemy migration: {e2}")
            raise
        
# Chamar a migração antes de db.create_all()
with app.app_context():
    migrate_database()
    db.create_all()

def filter_by_user(query, user_id=None):
    """Adiciona filtro de user_id a uma query"""
    if user_id is None:
        user_id = current_user.id
    return query.filter_by(user_id=user_id)
    
def get_next_bet_id():
    """Get the next available bet ID"""
    try:
        # Método 1: Usar SQL direto
        result = db.session.execute(db.text("SELECT MAX(id) FROM bet")).scalar()
        if result:
            return result + 1
        return 1
    except Exception:
        # Método 2: Fallback para SQLAlchemy
        last_bet = Bet.query.order_by(Bet.id.desc()).first()
        if last_bet:
            return last_bet.id + 1
        return 1
    
def get_active_bankroll():
    """Retorna o bankroll ativo do utilizador atual ou None"""
    if current_user.is_authenticated:
        return Bankroll.query.filter_by(user_id=current_user.id, is_active=True).first()
    return None     
    
# ====== GEMINI INTEGRATION (REST) ======

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
    prompt = build_gemini_prompt()

    with open(image_path, "rb") as f:
        image_bytes = f.read()
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": image_b64,
                        }
                    },
                ]
            }
        ]
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }

    last_error = None

    for attempt in range(max_retries):
        try:
            print("DEBUG GEMINI_ENDPOINT:", GEMINI_ENDPOINT)
            resp = requests.post(GEMINI_ENDPOINT, headers=headers, json=payload, timeout=30)
            print("DEBUG Gemini status code:", resp.status_code)

            if resp.status_code == 503:
                last_error = resp
                delay = base_delay * (attempt + 1)
                print(f"DEBUG Gemini 503, retrying in {delay} seconds...")
                time.sleep(delay)
                continue

            resp.raise_for_status()
            data = resp.json()
            print("DEBUG Gemini raw response:", resp.text)

            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError) as e:
                raise RuntimeError(f"Unexpected Gemini response structure: {data}") from e

            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`")
                if cleaned.lower().startswith("json"):
                    cleaned = cleaned[4:]
                cleaned = cleaned.strip()

            result = json.loads(cleaned)
            
            # Se a data não foi fornecida ou é inválida, usar data atual
            if 'placed_at' in result and result['placed_at']:
                try:
                    # Verificar se a data é válida
                    datetime.fromisoformat(result['placed_at'])
                except ValueError:
                    result['placed_at'] = None
            
            return result

        except requests.RequestException as e:
            last_error = e
            delay = base_delay * (attempt + 1)
            print(f"DEBUG Gemini request exception ({e}), retrying in {delay} seconds...")
            time.sleep(delay)

    # All retries failed
    raise RuntimeError(f"Gemini API unavailable after {max_retries} attempts: {last_error}")

def calculate_bookmaker_profit(bankroll_id, bookmaker_id):
    """Calcula o profit (que pode ser negativo) para um bookmaker"""
    balance_record = BankrollBookmakerBalance.query.filter_by(
        bankroll_id=bankroll_id,
        bookmaker_id=bookmaker_id
    ).first()
    
    if not balance_record:
        return 0.0
    
    # Profit = current_balance - starting_balance (pode ser negativo)
    profit = balance_record.current_balance - balance_record.starting_balance
    
    return round(profit, 2)

def calculate_bookmaker_balance(bankroll_id, bookmaker_id):
    """Calcula o balance (nunca negativo) e profit (pode ser negativo)"""
    balance_record = BankrollBookmakerBalance.query.filter_by(
        bankroll_id=bankroll_id,
        bookmaker_id=bookmaker_id
    ).first()
    
    if not balance_record:
        return 0.0, 0.0
    
    roll = Bankroll.query.get(bankroll_id)
    
    # Calcular balance (nunca negativo)
    deposits = sum(
        tx.amount for tx in roll.transactions
        if tx.bookmaker_id == bookmaker_id and tx.type == "deposit"
    )
    withdrawals = sum(
        tx.amount for tx in roll.transactions
        if tx.bookmaker_id == bookmaker_id and tx.type == "withdrawal"
    )
    
    # Impacto das apostas no balance
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
    
    balance = balance_record.starting_balance + deposits - withdrawals + bets_impact
    
    # Balance nunca pode ser negativo
    if balance < 0:
        balance = 0.0
    
    # Profit = balance - starting_balance (pode ser negativo)
    profit = balance - balance_record.starting_balance
    
    return balance, profit

def parse_bet_in_background(bet_id: int, image_path: str):
    from app import app, db, Bet, BetLeg  # or move function above

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

            # Clear existing legs, then add new ones
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
            
def update_bookmaker_balance(bankroll_id, bookmaker_id):
    """Atualiza o current_balance de um bookmaker num bankroll específico"""
    balance_record = BankrollBookmakerBalance.query.filter_by(
        bankroll_id=bankroll_id,
        bookmaker_id=bookmaker_id
    ).first()
    
    if not balance_record:
        # Criar se não existir
        balance_record = BankrollBookmakerBalance(
            bankroll_id=bankroll_id,
            bookmaker_id=bookmaker_id,
            starting_balance=0.0,
            current_balance=0.0
        )
        db.session.add(balance_record)
        db.session.flush()
    
    # Calcular balance a partir das transações
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
    
    # BALANCE NUNCA PODE SER NEGATIVO
    if current < 0:
        current = 0.0
    
    balance_record.current_balance = current
    db.session.commit()
    
    return balance_record   
         
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
    
    # ===== RECALCULAR APENAS AQUI =====
    # Isto só é chamado quando há uma nova transação
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

# ====== ROUTES ======

@app.route("/")
@login_required
def index():
    bets = Bet.query.filter_by(user_id=current_user.id).order_by(Bet.placed_at.desc()).all()
    return render_template("bets.html", bets=bets)

# --- GESTÃO DE APOSTAS ---
# app.py - Atualizar a rota bets_list para ordenar por data decrescente

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
    
# --- GESTÃO DE BANCAS ---
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
            user_id=current_user.id  # <-- ADICIONADO
        )
        db.session.add(new_b)
        db.session.flush()
        
        # Criar transações e balances para cada bookmaker
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
                    
                    # Transação de depósito
                    tx = Transaction(
                        bankroll_id=new_b.id,
                        type="deposit",
                        amount=amount,
                        bookmaker_id=int(bookmaker_id),
                        notes=f"Initial allocation to bookmaker"
                    )
                    db.session.add(tx)
                    
                    # Balance inicial por bookmaker neste bankroll
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

    rolls = Bankroll.query.order_by(Bankroll.created_at.asc()).all()
    books = Bookmaker.query.order_by(Bookmaker.name.asc()).all()
    
    # Calcular saldo atual
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
        
        # Calcular profit e invested
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
        
        # Agrupar por mês
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

    # Ordenar meses
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
            # ===== USAR O CURRENT_BALANCE GUARDADO =====
            # NÃO recalcular a partir das transações
            bookmaker_balances[book.id] = round(balance_record.current_balance, 2)
            print(f"Bookmaker {book.name}: current_balance = {balance_record.current_balance}")
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
    all_bankrolls = Bankroll.query.filter(Bankroll.id != roll.id).all()
    
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
    
@app.route("/bankrolls/<int:roll_id>/set_active", methods=["POST"])
@login_required
def set_active_bankroll(roll_id):
    # Desativar todos os bankrolls do utilizador
    Bankroll.query.filter_by(user_id=current_user.id).update({Bankroll.is_active: False})
    db.session.commit()
    
    # Ativar o selecionado
    roll = Bankroll.query.filter_by(id=roll_id, user_id=current_user.id).first_or_404()
    roll.is_active = True
    db.session.commit()
    
    flash(f"Bankroll '{roll.name}' set as active!", "success")
    referrer = request.referrer
    if referrer:
        return redirect(referrer)
    return redirect(url_for("bankrolls_list"))   
    
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
    
    if not from_bankroll or not to_bankroll:
        flash("Bankroll not found", "error")
        return redirect(request.referrer or url_for("bankrolls_list"))
    
    # Verificar saldo disponível no bankroll de origem
    net = sum(
        tx.amount if tx.type == "deposit" else -tx.amount
        for tx in from_bankroll.transactions
    )
    current_balance = from_bankroll.starting_balance + net
    if current_balance < 0:
        current_balance = 0.0
    
    if amount > current_balance:
        flash(f"Insufficient balance in {from_bankroll.name}. Available: {current_balance} {from_bankroll.currency}", "error")
        return redirect(request.referrer or url_for("bankrolls_list"))
    
    # Criar transação de saída (withdrawal) do bankroll de origem
    tx_out = Transaction(
        bankroll_id=from_bankroll.id,
        type="withdrawal",
        amount=amount,
        notes=f"Transfer to {to_bankroll.name}: {notes}",
    )
    db.session.add(tx_out)
    
    # Criar transação de entrada (deposit) no bankroll de destino
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

# --- GESTÃO DE CASAS DE APOSTAS ---
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
            user_id=current_user.id  # <-- ADICIONADO
        )
        db.session.add(new_bm)
        db.session.commit()
        flash("Bookmaker added!", "success")
        return redirect(url_for("bookmakers_list"))

    books = Bookmaker.query.order_by(Bookmaker.name.asc()).all()
    
    # Obter bankroll ativo
    active_bankroll = Bankroll.query.filter_by(is_active=True).first()
    
    # Obter todos os bankrolls para o dropdown
    all_bankrolls = Bankroll.query.order_by(Bankroll.name.asc()).all()
    selected_bankroll_id = request.args.get('bankroll_id', '')
    
    if selected_bankroll_id and selected_bankroll_id.isdigit():
        target_bankroll = Bankroll.query.get(int(selected_bankroll_id))
    else:
        target_bankroll = active_bankroll
    
    # Balanço por bookmaker - USAR O CURRENT_BALANCE DO RECORD
    balances = {}
    for book in books:
        if target_bankroll:
            # Buscar o balance record para este bookmaker neste bankroll
            balance_record = BankrollBookmakerBalance.query.filter_by(
                bankroll_id=target_bankroll.id,
                bookmaker_id=book.id
            ).first()
            
            if balance_record:
                # ===== USAR O CURRENT_BALANCE GUARDADO =====
                # NÃO RECALCULAR - usar o valor que foi guardado
                balances[book.id] = round(balance_record.current_balance, 2)
                print(f"Bookmaker {book.name}: current_balance = {balance_record.current_balance}")
            else:
                # Se não houver balance record, criar um com 0
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

@app.route("/bookmakers/<int:bankroll_id>")
def bookmakers_by_bankroll(bankroll_id):
    return redirect(url_for('bookmakers_list', bankroll_id=bankroll_id))    

@app.route('/save_manual_bet', methods=['POST'])
def save_manual_bet():
    bet_type = request.form.get('bet_type', 'single')
    status = request.form.get('status') or 'open'
    if status == 'cashout':
        status = 'cashed_out'

    bankroll_id = request.form.get('bankroll_id') or None
    notes = request.form.get('notes')
    
    # NOVOS CAMPOS
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

    # Se for freebet, o stake não sai do bankroll
    effective_stake = 0 if is_freebet else stake
    potential_return = round(effective_stake * total_odds, 2) if (effective_stake and total_odds) else None

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

@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        # Verificar se é upload de imagem ou formulário normal
        file = request.files.get("image")
        
        # Se não houver imagem, é um formulário normal (não deveria acontecer)
        if not file or file.filename == "":
            flash("No file uploaded", "error")
            return redirect(request.url)

        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}_{file.filename}"
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)

        # ===== OBTER PARÂMETROS DO FORMULÁRIO =====
        is_freebet = request.form.get('is_freebet') == 'on'
        is_live = request.form.get('is_live') == 'on'
        bankroll_id = request.form.get('bankroll_id')
        bookmaker_id = request.form.get('bookmaker_id')
        
        # Se for combined, usar o bookmaker_id_combined
        if not bookmaker_id:
            bookmaker_id = request.form.get('bookmaker_id_combined')
        
        print(f"DEBUG Upload:")
        print(f"  - is_freebet: {is_freebet}")
        print(f"  - is_live: {is_live}")
        print(f"  - bankroll_id: {bankroll_id}")
        print(f"  - bookmaker_id: {bookmaker_id}")
        
        # Se não houver bankroll_id, usar o ativo
        if not bankroll_id or bankroll_id == '':
            active = get_active_bankroll()
            if active:
                bankroll_id = str(active.id)
                print(f"  - Using active bankroll: {active.id}")
        
        # Se não houver bookmaker_id, tentar extrair do Gemini ou usar None
        try:
            gemini_data = call_gemini_on_betslip(filepath)
            print("DEBUG gemini_data:", gemini_data)
        except Exception as e:
            import traceback
            traceback.print_exc()
            flash(f"Error reading betslip with AI: {e}", "error")
            bet = Bet(
                id=get_next_bet_id(),
                image_path=filename,
                status="open",
                notes=f"AI parsing failed: {e}",
                is_freebet=is_freebet,
                is_live=is_live,
                bankroll_id=int(bankroll_id) if bankroll_id and bankroll_id.isdigit() else None,
                bookmaker_id=int(bookmaker_id) if bookmaker_id and bookmaker_id.isdigit() else None,
                placed_at=datetime.utcnow(),
            )
            db.session.add(bet)
            db.session.commit()
            flash(f"Bet #{bet.id} created with errors.", "warning")
            return redirect(url_for("edit_bet", bet_id=bet.id))

        parsed = parse_betslip_from_gemini(gemini_data)
        print("DEBUG parsed from Gemini:", parsed)

        # ---- Lógica de Bookmaker ----
        # Se o utilizador selecionou um bookmaker, usar esse
        if bookmaker_id and bookmaker_id.isdigit():
            matched_bookmaker_id = int(bookmaker_id)
            book = Bookmaker.query.get(matched_bookmaker_id)
            raw_bookmaker_name = book.name if book else None
            print(f"  - Using selected bookmaker: {raw_bookmaker_name} (ID: {matched_bookmaker_id})")
        else:
            # Tentar extrair do Gemini
            raw_bookmaker_name = parsed.get("bookmaker")
            matched_bookmaker_id = None
            
            if raw_bookmaker_name:
                clean_name = raw_bookmaker_name.strip()
                existing_bookmaker = Bookmaker.query.filter(
                    Bookmaker.name.ilike(clean_name)
                ).first()
                if existing_bookmaker:
                    matched_bookmaker_id = existing_bookmaker.id
                else:
                    new_book = Bookmaker(name=clean_name)
                    db.session.add(new_book)
                    db.session.flush()
                    matched_bookmaker_id = new_book.id
                print(f"  - Using AI detected bookmaker: {raw_bookmaker_name} (ID: {matched_bookmaker_id})")

        # Calcular stake
        stake = parsed.get("stake")
        total_odds = parsed.get("total_odds")
        potential_return = round(stake * total_odds, 2) if (stake and total_odds) else None

        # Processar a data
        placed_at = parsed.get("placed_at")
        if not placed_at:
            placed_at = datetime.utcnow()
        else:
            try:
                if placed_at.year < 2020 or placed_at.year > 2030:
                    placed_at = datetime.utcnow()
            except AttributeError:
                placed_at = datetime.utcnow()

        # ===== CRIAR A APOSTA COM TODOS OS CAMPOS =====
        bet = Bet(
            id=get_next_bet_id(),
            bookmaker=raw_bookmaker_name,
            bookmaker_id=matched_bookmaker_id,
            bankroll_id=int(bankroll_id) if bankroll_id and bankroll_id.isdigit() else None,
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
            is_freebet=is_freebet,  # <--- GUARDAR FREEBET
            is_live=is_live, # <--- GUARDAR LIVE
            user_id=current_user.id, 
        )
        db.session.add(bet)
        db.session.flush()

        # Adicionar legs
        legs: List[Dict[str, Any]] = parsed.get("legs") or []
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

        flash(f"✅ Bet #{bet.id} uploaded! Bookmaker: {raw_bookmaker_name or 'None'}, Freebet: {is_freebet}, Live: {is_live}", "success")
        return redirect(url_for("edit_bet", bet_id=bet.id))

    # GET - renderizar o formulário
    bankrolls = Bankroll.query.all()
    bookmakers = Bookmaker.query.all()
    active_bankroll = get_active_bankroll()
    
    return render_template("upload.html", 
                          bankrolls=bankrolls, 
                          bookmakers=bookmakers,
                          active_bankroll=active_bankroll)

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
        """Sincroniza as transações da aposta"""
        # Limpa transações automáticas anteriores desta aposta
        Transaction.query.filter_by(bet_id=bet_obj.id).delete()

        if not bet_obj.bankroll_id or not bet_obj.bookmaker_id or not bet_obj.stake:
            print("DEBUG sync_bet_transactions: dados insuficientes", flush=True)
            return

        if bet_obj.status == "open":
            return

        # ===== FREE BET =====
        if bet_obj.is_freebet:
            # Freebet: nunca perde dinheiro, apenas pode ganhar
            if bet_obj.status == "won" and bet_obj.potential_return:
                # Depósito do prémio (não há stake a deduzir)
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
            # Se perdeu, não há transações (não perde dinheiro)
            return

        # ===== APOSTA NORMAL =====
        # 1. Retirada do valor apostado (Stake)
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

        # 2. Depósito do prémio ou cashout
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
        print("DEBUG edit_bet: bet", bet.id,
              "old_status:", old_status,
              "form_status:", form_status,
              flush=True)

        # Bet fields
        bankroll_id = request.form.get("bankroll_id")
        bookmaker_id = request.form.get("bookmaker_id")
        bet.bankroll_id = int(bankroll_id) if bankroll_id else None
        bet.bookmaker_id = int(bookmaker_id) if bookmaker_id else None

        print("DEBUG edit_bet: bankroll_id =", bet.bankroll_id,
              "bookmaker_id =", bet.bookmaker_id, flush=True)

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

        # NOVOS CAMPOS
        bet.is_freebet = request.form.get('is_freebet') == '1'
        bet.is_live = request.form.get('is_live') == '1'

        placed_at_str = request.form.get("placed_at")
        if placed_at_str:
            try:
                bet.placed_at = datetime.fromisoformat(placed_at_str)
            except ValueError:
                pass

        # ---- Status: trust manual selection from the form ----
        if form_status:
            bet.status = form_status

        # Legs: status & builder flag
        legs = BetLeg.query.filter_by(bet_id=bet.id).all()
        for leg in legs:
            status_field = f"leg_status_{leg.id}"
            builder_field = f"leg_builder_{leg.id}"

            new_leg_status = request.form.get(status_field)
            if new_leg_status:
                leg.status = new_leg_status

            leg.is_builder = builder_field in request.form

        # If no manual status was provided, derive from legs
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

        # Recalculate total odds from legs that are not lost and have odds
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

        new_status = bet.status
        print("DEBUG edit_bet: new_status:", new_status,
              "stake:", bet.stake,
              "potential_return:", bet.potential_return,
              "cashed_out_amount:", bet.cashed_out_amount, flush=True)

        # ---- Sincronizar transações ----
        sync_bet_transactions(bet)

        db.session.commit()
        print("DEBUG edit_bet: committed", flush=True)
        flash("Bet and legs updated", "success")
        return redirect(url_for("bets_list"))

    # GET: load legs + lookup lists
    legs = BetLeg.query.filter_by(bet_id=bet.id).all()
    return render_template(
        "bet_detail.html",
        bet=bet,
        legs=legs,
        bankrolls=rolls,
        bookmakers=books,
    )

@app.route("/bets/<int:bet_id>/delete", methods=["POST"])
@login_required
def delete_bet(bet_id):
    bet = Bet.query.filter_by(id=bet_id, user_id=current_user.id).first_or_404()
    db.session.delete(bet)
    db.session.commit()
    flash(f"Bet #{bet_id} deleted successfully.", "success")
    return redirect(url_for("index"))

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
    bet.status = "cashed_out"  # you can add this to your status dropdown if you wish

    db.session.commit()
    flash(f"Bet #{bet.id} cashed out for {amount}", "success")
    return redirect(url_for("edit_bet", bet_id=bet.id))


@app.route("/bankrolls/<int:roll_id>/transaction", methods=["POST"])
def add_transaction(roll_id):
    roll = Bankroll.query.get_or_404(roll_id)
    tx_type = request.form.get("type")
    amount_raw = request.form.get("amount")
    notes = request.form.get("notes")
    bookmaker_id = request.form.get("bookmaker_id")

    try:
        amount = float(amount_raw.replace(",", "."))
    except (TypeError, ValueError):
        flash("Invalid amount", "error")
        return redirect(url_for("bankrolls"))

    tx = Transaction(
        bankroll_id=roll.id,
        type=tx_type,
        amount=amount,
        notes=notes,
        bookmaker_id=int(bookmaker_id) if bookmaker_id else None,
    )
    db.session.add(tx)
    db.session.commit()
    flash(f"{tx_type.capitalize()} recorded", "success")
    return redirect(url_for("bankrolls"))

@app.route("/bets/<int:bet_id>/quick_update", methods=["POST"])
@login_required
def quick_update_bet(bet_id):
    bet = Bet.query.filter_by(id=bet_id, user_id=current_user.id).first_or_404()

    # Update bet status if provided
    new_bet_status = request.form.get("bet_status")
    if new_bet_status:
        bet.status = new_bet_status

    # Update leg statuses if provided
    for leg in bet.legs:
        field_name = f"leg_status_{leg.id}"
        new_leg_status = request.form.get(field_name)
        if new_leg_status:
            leg.status = new_leg_status

    # Recalculate bet status from legs if any leg status changed
    has_lost = any(leg.status == "lost" for leg in bet.legs)
    all_won = bet.legs and all(leg.status == "won" for leg in bet.legs)

    if has_lost:
        bet.status = "lost"
    elif all_won:
        bet.status = "won"
    else:
        if not new_bet_status:
            bet.status = "open"

    # Synchronize transactions
    Transaction.query.filter_by(bet_id=bet.id).delete()
    
    # ===== FREE BET =====
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
        # Se perdeu, não faz transações
        db.session.commit()
        flash(f"Bet #{bet.id} updated from list.", "success")
        return redirect(url_for("index"))
    
    # ===== APOSTA NORMAL =====
    if bet.bankroll_id and bet.bookmaker_id and bet.stake and bet.status != "open":
        # Stake withdrawal
        if bet.status in ("won", "lost", "cashed_out"):
            db.session.add(Transaction(
                bankroll_id=bet.bankroll_id,
                bookmaker_id=bet.bookmaker_id,
                bet_id=bet.id,
                type="withdrawal",
                amount=bet.stake,
                notes=f"Stake for bet #{bet.id}"
            ))
        # Returns deposit
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

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/bankrolls/<int:roll_id>/bets")
def bets_for_bankroll(roll_id):
    roll = Bankroll.query.get_or_404(roll_id)
    bets = Bet.query.filter_by(bankroll_id=roll.id).order_by(Bet.placed_at.desc()).all()
    return render_template("bets.html", bets=bets, title=f"Bets for bankroll: {roll.name}")

# app.py - Adicionar rotas para editar/eliminar Bankrolls

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
    print(f"=== DELETANDO BANKROLL {roll_id} ===")
    roll = Bankroll.query.filter_by(id=roll_id, user_id=current_user.id).first_or_404()
    print(f"Bankroll encontrado: {roll.name}")
    
    # Verificar se há apostas associadas a este bankroll
    bets_count = Bet.query.filter_by(bankroll_id=roll.id).count()
    print(f"Apostas associadas: {bets_count}")
    
    if bets_count > 0:
        flash(f"Cannot delete bankroll '{roll.name}' because it has {bets_count} associated bets. Please reassign or delete the bets first.", "error")
        return redirect(url_for("bankrolls_list"))
    
    # Verificar se há transações associadas
    transactions_count = Transaction.query.filter_by(bankroll_id=roll.id).count()
    print(f"Transações associadas: {transactions_count}")
    
    # Se houver transações, apagar primeiro (cascade delete)
    if transactions_count > 0:
        print(f"Apagando {transactions_count} transações associadas...")
        Transaction.query.filter_by(bankroll_id=roll.id).delete()
        print("Transações apagadas!")
    
    # Apagar balances por bookmaker
    balances_count = BankrollBookmakerBalance.query.filter_by(bankroll_id=roll.id).count()
    if balances_count > 0:
        print(f"Apagando {balances_count} bookmaker balances...")
        BankrollBookmakerBalance.query.filter_by(bankroll_id=roll.id).delete()
        print("Balances apagados!")
    
    # Se o bankroll for o ativo, desativar antes de apagar
    if roll.is_active:
        roll.is_active = False
        db.session.commit()
        print("Bankroll desativado")
    
    roll_name = roll.name
    
    # Apagar o bankroll
    db.session.delete(roll)
    db.session.commit()
    print(f"Bankroll {roll_name} apagado!")
    
    # Definir novo ativo
    remaining = Bankroll.query.first()
    if remaining:
        remaining.is_active = True
        db.session.commit()
        print(f"Novo ativo: {remaining.name}")
    
    flash(f"Bankroll '{roll_name}' deleted successfully with {transactions_count} transactions and {balances_count} balances!", "success")
    return redirect(url_for("bankrolls_list"))

@app.route("/bookmakers/<int:book_id>/bets")
def bets_for_bookmaker(book_id):
    book = Bookmaker.query.get_or_404(book_id)
    bets = Bet.query.filter_by(bookmaker_id=book.id).order_by(Bet.placed_at.desc()).all()
    return render_template("bets.html", bets=bets, title=f"Bets for bookmaker: {book.name}")

# app.py - Adicionar rotas para editar/eliminar Bookmakers

@app.route("/bookmakers/<int:book_id>/edit", methods=["GET", "POST"])
@login_required
def edit_bookmaker(book_id):
    book = Bookmaker.query.filter_by(id=book_id, user_id=current_user.id).first_or_404()
    
    # Obter o bankroll selecionado (se houver)
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
        
        # Atualizar o bookmaker (nome e currency são globais)
        book.name = name
        book.currency = currency
        db.session.commit()
        
        # Atualizar o starting balance APENAS para este bankroll
        if bankroll_id:
            balance_record = BankrollBookmakerBalance.query.filter_by(
                bankroll_id=int(bankroll_id),
                bookmaker_id=book.id
            ).first()
            
            if balance_record:
                old_starting = balance_record.starting_balance
                old_current = balance_record.current_balance
                
                # Atualizar starting balance
                balance_record.starting_balance = starting_balance
                
                # Se o current_balance for igual ao old_starting, atualizar também
                # Caso contrário, manter o current_balance (o utilizador fez ajustes manuais)
                if old_current == old_starting:
                    balance_record.current_balance = starting_balance
                
                db.session.commit()
                flash(f"Bookmaker '{book.name}' updated with starting balance {starting_balance:.2f}€ for this bankroll!", "success")
            else:
                # Criar novo balance record
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
    
    # GET - mostrar formulário
    # Buscar o starting balance atual para este bankroll
    current_starting = 0.0
    if bankroll_id:
        balance_record = BankrollBookmakerBalance.query.filter_by(
            bankroll_id=int(bankroll_id),
            bookmaker_id=book.id
        ).first()
        if balance_record:
            current_starting = balance_record.starting_balance
    
    all_bankrolls = Bankroll.query.order_by(Bankroll.name.asc()).all()
    
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
    
    # Verificar se há apostas associadas
    bets_count = Bet.query.filter_by(bookmaker_id=book.id).count()
    if bets_count > 0:
        flash(f"Cannot delete bookmaker with {bets_count} associated bets.", "error")
        return redirect(url_for("bookmakers_list"))
    
    db.session.delete(book)
    db.session.commit()
    flash(f"Bookmaker '{book.name}' deleted.", "success")
    return redirect(url_for("bookmakers_list"))

# app.py - Adicionar estas funções antes da rota stats

def bet_profit(b: Bet) -> float:
    """Calcula o profit de uma aposta (freebet: só ganha, nunca perde)"""
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
    """Retorna o range de odds"""
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

def calculate_best_performance_period(bets):
    """Melhor período de performance (semana)"""
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

def calculate_most_profitable_sport(bets):
    """Desporto mais lucrativo"""
    sport_profit = {}
    for b in bets:
        key = b.sport or "Desconhecido"
        sport_profit[key] = sport_profit.get(key, 0) + bet_profit(b)
    
    if not sport_profit:
        return "N/A", 0
    
    best = max(sport_profit.items(), key=lambda x: x[1])
    return best[0], round(best[1], 2)

def calculate_best_bookmaker(bets):
    """Bookmaker mais lucrativo"""
    book_profit = {}
    for b in bets:
        key = b.bookmaker_obj.name if b.bookmaker_obj else (b.bookmaker or "Desconhecido")
        book_profit[key] = book_profit.get(key, 0) + bet_profit(b)
    
    if not book_profit:
        return "N/A", 0
    
    best = max(book_profit.items(), key=lambda x: x[1])
    return best[0], round(best[1], 2)

def calculate_monthly_performance(bets):
    """Performance mensal"""
    monthly = {}
    for b in bets:
        if b.placed_at:
            month_key = b.placed_at.strftime("%B %Y")
            monthly[month_key] = monthly.get(month_key, 0) + bet_profit(b)
    return monthly

def calculate_daily_performance(bets):
    """Performance diária"""
    daily = {}
    for b in bets:
        if b.placed_at:
            day_key = b.placed_at.strftime("%Y-%m-%d")
            daily[day_key] = daily.get(day_key, 0) + bet_profit(b)
    return daily

def calculate_drawdown(bets_list):
    """Calcula a máxima queda do bankroll"""
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

def calculate_sharpe_ratio(bets_list):
    """Calcula o Sharpe Ratio (risco/retorno)"""
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

def calculate_win_rate_by_odds_range(bets):
    """Win rate por range de odds"""
    ranges = {
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
            if range_key in ranges:
                ranges[range_key]['total'] += 1
                if b.status == 'won':
                    ranges[range_key]['won'] += 1
    
    return ranges

def calculate_best_streak(bets):
    """Calcula a melhor e pior sequência"""
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

@app.route("/stats")
@login_required
def stats():
    # 1. Obter parâmetros dos Filtros
    period = request.args.get("period", "all")
    selected_bankroll = request.args.get("bankroll_id", "")
    selected_sport = request.args.get("sport", "")
    
    if not selected_bankroll:
        active = get_active_bankroll()
        if active:
            selected_bankroll = str(active.id)

    # Base da query
    query = Bet.query.filter_by(user_id=current_user.id)

    # Filtro de Período
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

    # Filtros
    if selected_bankroll and selected_bankroll.isdigit():
        query = query.filter(Bet.bankroll_id == int(selected_bankroll))
    if selected_sport:
        query = query.filter(Bet.sport == selected_sport)

    bets = query.all()
    
    # Ordenações
    sorted_bets = sorted(bets, key=lambda x: x.placed_at or datetime.min)
    all_recent_bets = sorted(bets, key=lambda x: x.placed_at or datetime.min, reverse=True)

    # ===== CÁLCULOS DE KPIs =====
    total_bets_count = len(bets)
    total_staked = sum(b.stake or 0.0 for b in bets)

    won_bets = [b for b in bets if b.status == "won"]
    lost_bets = [b for b in bets if b.status == "lost"]
    cashed_out_bets = [b for b in bets if b.status == "cashed_out"]
    resolved_bets_count = len(won_bets) + len(lost_bets) + len(cashed_out_bets)

    win_rate = (len(won_bets) / resolved_bets_count * 100) if resolved_bets_count > 0 else 0.0

    valid_odds = [b.total_odds for b in bets if b.total_odds is not None]
    avg_odds = (sum(valid_odds) / len(valid_odds)) if valid_odds else 0.0

    # Profit
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

    # ===== MÉTRICAS AVANÇADAS =====
    drawdown = calculate_drawdown(bets)
    sharpe_ratio = calculate_sharpe_ratio(bets)
    best_streak, worst_streak = calculate_best_streak(bets)
    
    # Odds Win Rate
    odds_win_rate = calculate_win_rate_by_odds_range(bets)
    
    # Insights
    best_week, best_week_profit = calculate_best_performance_period(bets)
    best_sport, best_sport_profit = calculate_most_profitable_sport(bets)
    best_bookmaker, best_bookmaker_profit = calculate_best_bookmaker(bets)
    
    # Performance mensal e diária
    monthly_performance = calculate_monthly_performance(bets)
    daily_performance = calculate_daily_performance(bets)

    # ===== AGRUPAMENTOS =====
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

    # Bankroll Stats
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

    # Status Stats
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

    # ===== VARIÁVEIS PARA O TEMPLATE =====
    all_bankrolls = Bankroll.query.filter_by(user_id=current_user.id).all()
    all_sports = [
        s[0] for s in db.session.query(Bet.sport).filter_by(user_id=current_user.id).distinct().all()
        if s[0] is not None
    ]

    sport_roi = {}
    for sport, data in sport_stats.items():
        sport_roi[sport] = (data["profit"] / data["staked"] * 100) if data["staked"] > 0 else 0.0

    # Bankroll balances
    current_balances = {}
    for roll in all_bankrolls:
        net = sum(tx.amount if tx.type == "deposit" else -tx.amount for tx in roll.transactions)
        current_balances[roll.id] = round(roll.starting_balance + net, 2)

    total_balance = sum(current_balances.values())

    # Odds distribution
    odds_distribution = {
        "1.0-1.5": 0, "1.5-2.0": 0, "2.0-2.5": 0,
        "2.5-3.0": 0, "3.0-4.0": 0, "4.0+": 0
    }
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
        monthly_performance=monthly_performance,
        daily_performance=daily_performance,
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
    
# app.py - Adicionar no final, antes do if __name__ == "__main__"

# ====== ROTAS PARA TRANSAÇÕES DE BANKROLL ======

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
    
    # Calcular saldo atual
    net = sum(
        tx.amount if tx.type == "deposit" else -tx.amount
        for tx in roll.transactions
    )
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

# ====== ROTAS PARA EXPORTAR/IMPORTAR DADOS ======

@app.route("/export/bets")
@login_required
def export_bets():
    bets = Bet.query.filter_by(user_id=current_user.id).order_by(Bet.placed_at.desc()).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Cabeçalhos
    writer.writerow([
        "ID", "Bookmaker", "Bankroll", "Sport", "Market Type", 
        "Total Odds", "Stake", "Potential Return", "Currency", 
        "Status", "Placed At", "Notes"
    ])
    
    # Dados
    for bet in bets:
        writer.writerow([
            bet.id,
            bet.bookmaker_obj.name if bet.bookmaker_obj else bet.bookmaker,
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
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'bets_export_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.csv'
    )

@app.route("/export/bankrolls")
@login_required
def export_bankrolls():
    bankrolls = Bankroll.query.filter_by(user_id=current_user.id).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Cabeçalhos
    writer.writerow([
        "Bankroll ID", "Bankroll Name", "Currency", "Starting Balance",
        "Transaction ID", "Transaction Type", "Amount", "Bookmaker", 
        "Notes", "Transaction Date"
    ])
    
    # Dados
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
            # Bankroll sem transações
            writer.writerow([
                roll.id,
                roll.name,
                roll.currency,
                roll.starting_balance,
                "", "", "", "", "", ""
            ])
    
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
    
    # Cabeçalhos
    writer.writerow([
        "ID", "Name", "Currency", "Starting Balance", "Created At"
    ])
    
    # Dados
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
def export_all():
    """Export all data - dashboard page"""
    return render_template("export.html")

@app.route("/import", methods=["GET", "POST"])
@login_required
def import_data():
    bankrolls = Bankroll.query.filter_by(user_id=current_user.id).order_by(Bankroll.name.asc()).all()
    
    if request.method == "POST":
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("No file uploaded", "error")
            return redirect(request.url)
        
        bankroll_id = request.form.get("bankroll_id")
        if not bankroll_id:
            flash("Please select a bankroll for the imported bets", "error")
            return redirect(request.url)
        
        try:
            bankroll_id = int(bankroll_id)
            bankroll = Bankroll.query.get(bankroll_id)
            if not bankroll:
                flash("Selected bankroll not found", "error")
                return redirect(request.url)
        except ValueError:
            flash("Invalid bankroll selection", "error")
            return redirect(request.url)
        
        try:
            content = file.read().decode('utf-8-sig')
            lines = content.splitlines()
            
            imported_count = 0
            errors = []
            current_bet = None
            current_legs = []
            reading_data = False
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                # Pular cabeçalhos e metadados
                if line.startswith('METRIKA') or line.startswith('Bets List') or line.startswith('Period;') or line.startswith('Generated;'):
                    continue
                if line.startswith('Total bets') or line.startswith('Simple bets') or line.startswith('Combined bets'):
                    continue
                if line.startswith('TOTALS;'):
                    continue
                if line.startswith('#;Date;Type;Event;Selection;Market;Sport;Bookmaker;Odds;Stake (€);Result;Profit (€)'):
                    reading_data = True
                    continue
                
                if not reading_data:
                    continue
                
                parts = line.split(';')
                
                # Verificar se é uma linha de aposta (começa com número)
                if parts[0].strip().isdigit():
                    # Salvar aposta anterior se existir
                    if current_bet:
                        save_bet_with_legs(current_bet, current_legs, bankroll_id)
                        imported_count += 1
                    
                    # Nova aposta
                    current_bet = parse_bet_row(parts)
                    current_legs = []
                    
                    # Verificar se a primeira linha já tem dados de leg
                    if len(parts) > 11 and parts[11] and parts[11].strip():
                        # Esta linha tem dados de leg na mesma linha (profit pode ser leg data)
                        leg = create_leg_from_row(parts, 1)
                        if leg:
                            current_legs.append(leg)
                
                else:
                    # É uma linha de leg (sem número)
                    if current_bet:
                        leg = create_leg_from_row(parts, 0)
                        if leg:
                            current_legs.append(leg)
            
            # Salvar última aposta
            if current_bet:
                save_bet_with_legs(current_bet, current_legs, bankroll_id)
                imported_count += 1
            
            db.session.commit()
            
            if errors:
                flash(f"Imported {imported_count} bets with {len(errors)} errors.", "warning")
                for error in errors[:5]:
                    flash(f"Error: {error}", "error")
            else:
                flash(f"Successfully imported {imported_count} bets to '{bankroll.name}'!", "success")
            
            return redirect(url_for("bets_list"))
            
        except Exception as e:
            db.session.rollback()
            flash(f"Error reading file: {str(e)}", "error")
            return redirect(request.url)
    
    return render_template("import_data.html", bankrolls=bankrolls)

def parse_bet_row(parts):
    """Parse a bet row from CSV"""
    bet_data = {
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
    return bet_data

def create_leg_from_row(parts, offset):
    """Create a leg from CSV row parts"""
    leg = {}
    
    # Os dados da leg podem estar em diferentes posições
    # No formato do ficheiro METRIKA, as legs têm: Event;Selection;Market;Sport;Bookmaker;Result
    # Mas também podem ter odds e stake vazios
    
    if len(parts) >= 6:
        leg['event'] = parts[0].strip() if parts[0].strip() else None
        leg['selection'] = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        leg['market'] = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        leg['sport'] = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None
        leg['bookmaker'] = parts[4].strip() if len(parts) > 4 and parts[4].strip() else None
        leg['result'] = parts[5].strip() if len(parts) > 5 and parts[5].strip() else None
        
        # Se tiver odds (posição 6 pode ser odds)
        if len(parts) > 6 and parts[6].strip():
            try:
                leg['odds'] = float(parts[6].strip().replace(',', '.'))
            except ValueError:
                leg['odds'] = None
        else:
            leg['odds'] = None
        
        # Verificar se a leg tem dados válidos
        if leg['event'] or leg['selection'] or leg['market']:
            return leg
    
    return None

def save_bet_with_legs(bet_data, legs_data, bankroll_id):
    """Save a bet with its legs to the database"""
    from datetime import datetime
    
    # Determinar o tipo de aposta
    bet_type = bet_data.get('type', 'Simple')
    is_combined = 'Combined' in bet_type
    
    # Parse da data
    placed_at = None
    if bet_data.get('date'):
        try:
            # Formato: DD/MM/YYYY
            placed_at = datetime.strptime(bet_data['date'], '%d/%m/%Y')
        except ValueError:
            placed_at = datetime.utcnow()
    
    # Parse do stake
    stake = None
    if bet_data.get('stake'):
        try:
            stake = float(bet_data['stake'].replace(' €', '').replace(',', '.').strip())
        except ValueError:
            stake = None
    
    # Parse das odds
    total_odds = None
    if bet_data.get('odds'):
        try:
            total_odds = float(bet_data['odds'].replace(',', '.').strip())
        except ValueError:
            total_odds = None
    
    # Parse do profit
    profit = None
    if bet_data.get('profit'):
        try:
            profit = float(bet_data['profit'].replace(' €', '').replace(',', '.').strip())
        except ValueError:
            profit = None
    
    # Determinar o status
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
    
    # Encontrar ou criar bookmaker
    bookmaker_id = None
    bookmaker_name = bet_data.get('bookmaker', '').strip()
    if bookmaker_name:
        existing = Bookmaker.query.filter(Bookmaker.name.ilike(bookmaker_name)).first()
        if existing:
            bookmaker_id = existing.id
        else:
            new_book = Bookmaker(name=bookmaker_name)
            db.session.add(new_book)
            db.session.flush()
            bookmaker_id = new_book.id
    
    # Calcular potential return
    potential_return = None
    if stake and total_odds:
        potential_return = stake * total_odds
    
    # Criar a aposta com o bankroll_id selecionado
    bet = Bet(
        id=get_next_bet_id(),  # <-- ADICIONADO
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
        user_id=current_user.id,
    )
    db.session.add(bet)
    db.session.flush()
    
    # Adicionar legs
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
    
    # Se a aposta estiver resolvida, criar transações
    if status in ('won', 'lost', 'cashed_out') and bankroll_id and bookmaker_id and stake:
        # Transação de stake (saída)
        db.session.add(Transaction(
            bankroll_id=bankroll_id,
            bookmaker_id=bookmaker_id,
            bet_id=bet.id,
            type='withdrawal',
            amount=stake,
            notes=f"Stake for bet #{bet.id}"
        ))
        
        # Transação de retorno (se ganhou)
        if status == 'won' and potential_return:
            db.session.add(Transaction(
                bankroll_id=bankroll_id,
                bookmaker_id=bookmaker_id,
                bet_id=bet.id,
                type='deposit',
                amount=potential_return,
                notes=f"Payout for bet #{bet.id}"
            ))

def parse_amount(value):
    """Parse amount string with currency symbols"""
    if not value:
        return None
    # Remover € e espaços, substituir vírgula por ponto
    cleaned = value.replace('€', '').replace(' ', '').replace(',', '.').strip()
    try:
        return float(cleaned)
    except ValueError:
        return None

def extract_legs_from_row(row, bet_data):
    """Extract leg data from a row, handling different formats"""
    legs = []
    
    # Formato 1: Leg data starts at position 12+ (if bet has multiple legs in one row)
    if len(row) > 12:
        # Processar como leg
        leg = create_leg_from_row(row, 1)
        if leg:
            legs.append(leg)
    
    # Formato 2: Leg data is in the main fields
    if bet_data:
        # Tentar extrair legs do evento/seleção se for Combined
        if 'Combined' in (bet_data.get('type') or ''):
            # Algumas combined bets têm os dados da leg no campo Event/Selection
            pass
    
    return legs

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
    
@app.route("/recalculate_stats", methods=["POST"])
def recalculate_stats():
    """Recalcula todas as estatísticas (útil após eliminações em massa)"""
    # Esta rota pode ser usada para forçar a atualização de caches
    flash("Statistics recalculated successfully!", "success")
    return redirect(request.referrer or url_for("index"))    

@app.route("/bookmakers/<int:book_id>/update_balance", methods=["POST"])
@login_required
def update_bookmaker_balance_manual(book_id):
    print("=" * 50)
    print("UPDATE BALANCE - ROTA CHAMADA")
    print(f"Book ID: {book_id}")
    
    book = Bookmaker.query.filter_by(id=book_id, user_id=current_user.id).first_or_404()
    print(f"Bookmaker: {book.name}")
    
    bankroll_id = request.form.get("bankroll_id")
    new_balance_raw = request.form.get("new_balance")
    notes = request.form.get("notes") or f"Manual balance adjustment for {book.name}"
    
    print(f"Bankroll ID: {bankroll_id}")
    print(f"New Balance Raw: {new_balance_raw}")
    print(f"Notes: {notes}")
    
    if not bankroll_id:
        print("ERRO: Bankroll ID não fornecido")
        flash("Bankroll is required.", "error")
        return redirect(url_for("bookmakers_list"))
    
    try:
        new_balance = float(new_balance_raw.replace(",", "."))
        print(f"New Balance parsed: {new_balance}")
    except (TypeError, ValueError) as e:
        print(f"ERRO ao parsear balance: {e}")
        flash("Invalid balance value.", "error")
        return redirect(url_for("bookmakers_list"))
    
    if new_balance < 0:
        print("ERRO: Balance negativo")
        flash("Balance cannot be negative.", "error")
        return redirect(url_for("bookmakers_list"))
    
    # Buscar o balance record
    balance_record = BankrollBookmakerBalance.query.filter_by(
        bankroll_id=int(bankroll_id),
        bookmaker_id=book_id
    ).first()
    
    if not balance_record:
        print("ERRO: Balance record não encontrado - criando novo")
        balance_record = BankrollBookmakerBalance(
            bankroll_id=int(bankroll_id),
            bookmaker_id=book_id,
            starting_balance=0.0,
            current_balance=0.0
        )
        db.session.add(balance_record)
        db.session.flush()
    
    print(f"Balance record encontrado:")
    print(f"  - Starting Balance: {balance_record.starting_balance}")
    print(f"  - Current Balance: {balance_record.current_balance}")
    
    current_balance = balance_record.current_balance
    
    print(f"Current Balance: {current_balance}")
    print(f"New Balance: {new_balance}")
    
    if new_balance == current_balance:
        print("Sem alteração no balance")
        flash("No change in balance.", "info")
        return redirect(url_for("bookmakers_list", bankroll_id=bankroll_id))
    
    # ===== ATUALIZAR DIRETAMENTE O CURRENT_BALANCE =====
    # Não criar transação - apenas atualizar o valor
    balance_record.current_balance = new_balance
    print(f"Balance record atualizado para: {new_balance}")
    
    db.session.commit()
    print("COMMIT realizado com sucesso!")
    print("=" * 50)
    
    flash(f"Balance for {book.name} corrected from {current_balance:.2f}€ to {new_balance:.2f}€!", "success")
    return redirect(url_for("bookmakers_list", bankroll_id=bankroll_id))

@app.context_processor
def utility_processor():
    """Disponibiliza variáveis globais para todos os templates"""
    from datetime import datetime
    
    active_bankroll = get_active_bankroll()
    all_bankrolls = Bankroll.query.order_by(Bankroll.name.asc()).all()
    
    return {
        'active_bankroll': active_bankroll,
        'all_bankrolls_global': all_bankrolls,
        'now': datetime.utcnow()
    }

@app.route("/register", methods=["GET", "POST"])
def register():
    # Se o utilizador já estiver autenticado, redirecionar
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        
        # Validar campos obrigatórios
        if not username or not email or not password:
            flash("All fields are required", "error")
            return render_template("register.html")
        
        # Validar tamanho do username
        if len(username) < 3 or len(username) > 20:
            flash("Username must be between 3 and 20 characters", "error")
            return render_template("register.html")
        
        # Validar formato do email
        if '@' not in email or '.' not in email:
            flash("Please enter a valid email address", "error")
            return render_template("register.html")
        
        # Validar password
        if len(password) < 6:
            flash("Password must be at least 6 characters", "error")
            return render_template("register.html")
        
        if password != confirm_password:
            flash("Passwords do not match", "error")
            return render_template("register.html")
        
        # Verificar se username já existe
        if User.query.filter_by(username=username).first():
            flash("Username already taken", "error")
            return render_template("register.html")
        
        # Verificar se email já existe
        if User.query.filter_by(email=email).first():
            flash("Email already registered", "error")
            return render_template("register.html")
        
        # Criar utilizador
        user = User(username=username, email=email, is_active=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        # Log de registo
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

@app.route("/login", methods=["GET", "POST"])
def login():
    # Se o utilizador já estiver autenticado, redirecionar
    if current_user.is_authenticated:
        app.logger.info(f"🔄 User already authenticated, redirecting to index: {current_user.username}")
        return redirect(url_for("index"))
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        
        # Validar campos
        if not username or not password:
            app.logger.warning(f"⚠️ Login attempt with empty fields | IP: {request.remote_addr}")
            flash("Please fill in all fields", "error")
            return render_template("login.html")
        
        # Buscar utilizador
        user = User.query.filter_by(username=username).first()
        
        # Verificar credenciais
        if not user:
            app.logger.warning(f"⚠️ Login failed - User not found: {username} | IP: {request.remote_addr}")
            flash("Invalid username or password", "error")
            return render_template("login.html")
        
        if not user.is_active:
            app.logger.warning(f"⚠️ Login failed - Account disabled: {username} | IP: {request.remote_addr}")
            flash("Your account has been disabled. Please contact support.", "error")
            return render_template("login.html")
        
        if not user.check_password(password):
            app.logger.warning(f"⚠️ Login failed - Wrong password: {username} | IP: {request.remote_addr}")
            flash("Invalid username or password", "error")
            return render_template("login.html")
        
        # Login bem-sucedido
        login_user(user, remember=True)
        
        # Log de login bem-sucedido
        UserLog.log(
            user_id=user.id,
            action="login",
            details=f"User logged in from {request.remote_addr}",
            request=request
        )
        app.logger.info(f"✅ Login successful: {user.username} (ID: {user.id}) | IP: {request.remote_addr}")
        
        # Verificar se há um next URL (redirecionamento após login)
        next_url = request.args.get('next')
        if next_url and next_url.startswith('/'):
            app.logger.info(f"🔄 Redirecting to next: {next_url}")
            return redirect(next_url)
        
        flash(f"Welcome back, {user.username}! 👋", "success")
        return redirect(url_for("index"))
    
    # GET - Mostrar página de login
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out successfully", "success")
    return redirect(url_for("index"))

@app.route("/profile")
@login_required
def profile():
    # Estatísticas do utilizador
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

@app.route("/tips")
@login_required
def tips_feed():
    """Feed de tips públicos"""
    # Tips públicas (não do utilizador atual)
    tips = Tip.query.filter(
        Tip.is_public == True,
        Tip.user_id != current_user.id
    ).order_by(Tip.created_at.desc()).all()
    
    return render_template("tips_feed.html", tips=tips)

@app.route("/tips/my")
@login_required
def my_tips():
    """Tips do utilizador atual"""
    tips = Tip.query.filter_by(user_id=current_user.id).order_by(Tip.created_at.desc()).all()
    return render_template("my_tips.html", tips=tips)

@app.route("/tips/create/<int:bet_id>", methods=["GET", "POST"])
@login_required
def create_tip(bet_id):
    """Criar tip a partir de uma bet"""
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
    """Dar like numa tip"""
    tip = Tip.query.get_or_404(tip_id)
    tip.likes += 1
    db.session.commit()
    return jsonify({'likes': tip.likes})

@app.route("/api/subscribe", methods=["POST"])
@login_required
def subscribe_push():
    data = request.get_json()
    # Guardar subscription no banco de dados
    # subscription = PushSubscription.query.filter_by(user_id=current_user.id).first()
    # if not subscription:
    #     subscription = PushSubscription(user_id=current_user.id, data=json.dumps(data))
    # else:
    #     subscription.data = json.dumps(data)
    # db.session.commit()
    return jsonify({'status': 'success'})

# app.py - Admin dashboard

@app.route("/admin")
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        flash("Access denied", "error")
        return redirect(url_for("index"))
    
    # Estatísticas
    total_users = User.query.count()
    total_bets = Bet.query.count()
    total_tips = Tip.query.count()
    total_bankrolls = Bankroll.query.count()
    total_logs = UserLog.query.count()
    
    # Utilizadores ativos (últimos 30 dias)
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    active_users = User.query.filter(User.created_at >= thirty_days_ago).count()
    
    # Últimos logs
    recent_logs = UserLog.query.order_by(UserLog.created_at.desc()).limit(50).all()
    
    # Estatísticas de logs por ação
    action_stats = db.session.query(
        UserLog.action, 
        db.func.count(UserLog.id)
    ).group_by(UserLog.action).all()
    
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
    
    # Parâmetros de filtro
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

@app.route("/admin/health")
def health_check():
    """Health check para monitorização"""
    status = {
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'version': '1.0.0'
    }
    
    # Verificar base de dados
    try:
        db.session.execute(db.text('SELECT 1'))
        status['database'] = 'connected'
    except Exception as e:
        status['database'] = f'error: {str(e)}'
        status['status'] = 'unhealthy'
    
    return jsonify(status)

# app.py - Adicionar rotas de admin

@app.route("/admin/logs/download")
@login_required
def download_logs():
    if not current_user.is_admin:
        flash("Access denied", "error")
        return redirect(url_for("index"))
    
    import io
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

@app.route("/value_bets")
@login_required
def value_bets():
    """Mostra apostas com valor detectado"""
    from value_detector import ValueBetDetector
    
    # Verificar se a API key está configurada
    import os
    use_api = os.environ.get('ODDS_API_KEY') is not None
    
    # Carregar bets do utilizador dentro do contexto da aplicação
    bets = Bet.query.filter_by(user_id=current_user.id).all()
    
    # Criar o detector passando a lista de bets já carregadas
    detector = ValueBetDetector(bets, use_api=use_api)
    
    # Buscar apostas abertas
    open_bets = [b for b in bets if b.status == 'open']
    
    value_bets = []
    for bet in open_bets:
        result = detector.detect_value(bet)
        if result:
            value_bets.append({
                'bet': bet,
                'value': result
            })
    
    # Ordenar por value_pct (maior primeiro)
    value_bets.sort(key=lambda x: x['value']['value_pct'], reverse=True)
    
    # Estatísticas
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
    """API para value bets (JSON)"""
    from value_detector import ValueBetDetector
    
    import os
    use_api = os.environ.get('ODDS_API_KEY') is not None
    
    bets = Bet.query.filter_by(user_id=current_user.id).all()
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
    
    return jsonify({
        'value_bets': result,
        'count': len(result)
    })

@app.route("/value_bets/simulate/<int:bet_id>", methods=["POST"])
@login_required
def simulate_value_bet(bet_id):
    """Simula o resultado de uma value bet"""
    bet = Bet.query.filter_by(id=bet_id, user_id=current_user.id).first_or_404()
    
    from value_detector import ValueBetDetector
    import random
    
    import os
    use_api = os.environ.get('ODDS_API_KEY') is not None
    
    # Carregar todas as bets para contexto histórico
    bets = Bet.query.filter_by(user_id=current_user.id).all()
    detector = ValueBetDetector(bets, use_api=use_api)
    
    result = detector.detect_value(bet)
    
    if not result:
        return jsonify({'error': 'No value detected'}), 400
    
    # Simular vários cenários
    simulations = []
    for i in range(100):
        if random.random() < (result['real_prob'] / 100):
            simulations.append('win')
        else:
            simulations.append('loss')
    
    wins = simulations.count('win')
    losses = simulations.count('loss')
    
    # Calcular profit esperado
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

# app.py - Adicionar após a criação do app
@app.context_processor
def inject_app_config():
    """Disponibiliza variáveis da app em todos os templates"""
    return {
        'APP_NAME': app.config['APP_NAME'],
        'APP_TAGLINE': app.config['APP_TAGLINE'],
        'APP_LOGO': app.config['APP_LOGO'],
        'APP_FAVICON': app.config['APP_FAVICON'],
        'APP_COLOR': app.config['APP_COLOR']
    }
                    
# app.py - Adicionar no final
if __name__ == "__main__":
    # Em produção, o Gunicorn gerencia a app
    # Em desenvolvimento, usar debug=True
    import os
    debug = os.environ.get('FLASK_DEBUG', 'False') == 'True'
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=debug)