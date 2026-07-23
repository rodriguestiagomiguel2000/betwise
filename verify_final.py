# verify_final.py
from app import app, db, User, Bankroll, Bookmaker, Bet

with app.app_context():
    tiago = User.query.filter_by(username='tiago32rodriguez').first()
    
    if tiago:
        print(f"👤 Utilizador: {tiago.username} (ID: {tiago.id})")
        print()
        print(f"🏦 Bankrolls: {Bankroll.query.filter_by(user_id=tiago.id).count()}")
        print(f"📚 Bookmakers: {Bookmaker.query.filter_by(user_id=tiago.id).count()}")
        print(f"🎲 Bets: {Bet.query.filter_by(user_id=tiago.id).count()}")
        print()
        print("✅ Agora os teus dados estão associados ao teu utilizador!")
    else:
        print("❌ Utilizador tiago32rodriguez não encontrado!")