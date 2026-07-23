# check_data.py
from app import app, db, Bankroll, Bookmaker, Bet, User

with app.app_context():
    print("=" * 50)
    print("📊 DATA CHECK")
    print("=" * 50)
    
    users = User.query.all()
    print(f"👤 Users: {len(users)}")
    for u in users:
        print(f"   - {u.username} (ID: {u.id})")
    
    print()
    print(f"🏦 Bankrolls total: {Bankroll.query.count()}")
    print(f"   - With user_id: {Bankroll.query.filter(Bankroll.user_id.isnot(None)).count()}")
    print(f"   - Without user_id: {Bankroll.query.filter(Bankroll.user_id.is_(None)).count()}")
    
    print()
    print(f"📚 Bookmakers total: {Bookmaker.query.count()}")
    print(f"   - With user_id: {Bookmaker.query.filter(Bookmaker.user_id.isnot(None)).count()}")
    print(f"   - Without user_id: {Bookmaker.query.filter(Bookmaker.user_id.is_(None)).count()}")
    
    print()
    print(f"🎲 Bets total: {Bet.query.count()}")
    print(f"   - With user_id: {Bet.query.filter(Bet.user_id.isnot(None)).count()}")
    print(f"   - Without user_id: {Bet.query.filter(Bet.user_id.is_(None)).count()}")