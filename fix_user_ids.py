# fix_user_ids.py
from app import app, db, User, Bankroll, Bookmaker, Bet, Transaction

with app.app_context():
    admin = User.query.filter_by(username='admin').first()
    if not admin:
        print("❌ Admin user not found! Please run create_admin.py first.")
        exit()
    
    admin_id = admin.id
    print(f"✅ Using admin ID: {admin_id}")
    
    # Atualizar Bankrolls
    count = Bankroll.query.filter(Bankroll.user_id.is_(None)).update({Bankroll.user_id: admin_id})
    print(f"✅ Updated {count} bankrolls")
    
    # Atualizar Bookmakers
    count = Bookmaker.query.filter(Bookmaker.user_id.is_(None)).update({Bookmaker.user_id: admin_id})
    print(f"✅ Updated {count} bookmakers")
    
    # Atualizar Bets
    count = Bet.query.filter(Bet.user_id.is_(None)).update({Bet.user_id: admin_id})
    print(f"✅ Updated {count} bets")
    
    db.session.commit()
    print("✅ Migration completed successfully!")