# create_admin.py
from app import app, db, User
from werkzeug.security import generate_password_hash

with app.app_context():
    # Verificar se já existe
    admin = User.query.filter_by(username='admin').first()
    if not admin:
        admin = User(
            username='admin',
            email='admin@metrikatips.com',
            password_hash=generate_password_hash('admin123'),
            is_admin=True,
            is_active=True
        )
        db.session.add(admin)
        db.session.commit()
        print("✅ Admin user created! (username: admin, password: admin123)")
    else:
        print("✅ Admin user already exists.")