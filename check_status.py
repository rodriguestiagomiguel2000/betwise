# check_status.py
from app import app, db, User, Bankroll, Bookmaker, Bet

with app.app_context():
    print("=" * 50)
    print("📊 VERIFICAR ESTADO")
    print("=" * 50)
    
    # Users
    users = User.query.all()
    print(f"👤 Users: {len(users)}")
    for u in users:
        print(f"   - {u.username} (ID: {u.id})")
    
    print()
    
    # Bankrolls
    total = Bankroll.query.count()
    with_user = Bankroll.query.filter(Bankroll.user_id.isnot(None)).count()
    without_user = Bankroll.query.filter(Bankroll.user_id.is_(None)).count()
    print(f"🏦 Bankrolls: {total} total")
    print(f"   - Com user_id: {with_user}")
    print(f"   - Sem user_id: {without_user}")
    if without_user > 0:
        print("   ❌ Há bankrolls sem user_id!")
    
    print()
    
    # Bookmakers
    total = Bookmaker.query.count()
    with_user = Bookmaker.query.filter(Bookmaker.user_id.isnot(None)).count()
    without_user = Bookmaker.query.filter(Bookmaker.user_id.is_(None)).count()
    print(f"📚 Bookmakers: {total} total")
    print(f"   - Com user_id: {with_user}")
    print(f"   - Sem user_id: {without_user}")
    if without_user > 0:
        print("   ❌ Há bookmakers sem user_id!")
    
    print()
    
    # Bets
    total = Bet.query.count()
    with_user = Bet.query.filter(Bet.user_id.isnot(None)).count()
    without_user = Bet.query.filter(Bet.user_id.is_(None)).count()
    print(f"🎲 Bets: {total} total")
    print(f"   - Com user_id: {with_user}")
    print(f"   - Sem user_id: {without_user}")
    if without_user > 0:
        print("   ❌ Há bets sem user_id!")