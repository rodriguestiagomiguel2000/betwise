# verify_fix.py
from app import app, db, User, Bankroll, Bookmaker, Bet

with app.app_context():
    print("=" * 50)
    print("📊 VERIFICAR CORREÇÃO")
    print("=" * 50)
    
    admin = User.query.filter_by(username='admin').first()
    if admin:
        print(f"👤 Admin: {admin.username} (ID: {admin.id})")
    
    print()
    
    # Bankrolls
    total = Bankroll.query.count()
    with_user = Bankroll.query.filter(Bankroll.user_id == admin.id).count()
    print(f"🏦 Bankrolls: {total} total")
    print(f"   - Do admin: {with_user}")
    
    # Bookmakers
    total = Bookmaker.query.count()
    with_user = Bookmaker.query.filter(Bookmaker.user_id == admin.id).count()
    print(f"📚 Bookmakers: {total} total")
    print(f"   - Do admin: {with_user}")
    
    # Bets
    total = Bet.query.count()
    with_user = Bet.query.filter(Bet.user_id == admin.id).count()
    print(f"🎲 Bets: {total} total")
    print(f"   - Do admin: {with_user}")
    
    # Mostrar alguns bankrolls do admin
    print("\n📋 Bankrolls do admin:")
    for b in Bankroll.query.filter(Bankroll.user_id == admin.id).limit(5).all():
        print(f"   - {b.name} (ID: {b.id})")