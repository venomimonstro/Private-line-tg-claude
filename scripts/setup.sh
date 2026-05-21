#!/bin/bash
set -e
echo "=== Private Line Setup ==="

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example — please fill in the values"
fi

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

echo "Generating encryption key..."
python3 -c "from cryptography.fernet import Fernet; print('ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
echo "Generating secret key..."
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))"

echo ""
echo "=== Setup complete ==="
echo "1. Edit .env with your values"
echo "2. Run: alembic upgrade head"
echo "3. Run: python scripts/create_admin.py"
echo "4. Run: python bot/main.py"
