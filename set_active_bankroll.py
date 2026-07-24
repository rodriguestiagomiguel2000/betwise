# set_active_bankroll.py
from app import app, db, Bankroll

with app.app_context():
    # Encontrar o primeiro bankroll
    first = Bankroll.query.first()
    if first:
        first.is_active = True
        db.session.commit()
        print(f"✅ Bankroll '{first.name}' definido como ativo!")
    else:
        print("❌ Nenhum bankroll encontrado. Importa dados primeiro.")