import os
from dotenv import load_dotenv


load_dotenv()


class Settings:
    DATABASE_URL: str = os.getenv('DATABASE_URL', 'sqlite+aiosqlite:///./dev.db')
    REDIS_URL: str = os.getenv('REDIS_URL', 'redis://localhost:6379/0')

    JWT_SECRET: str = os.getenv('JWT_SECRET', 'replace-with-very-strong-secret')
    JWT_ALGORITHM: str = os.getenv('JWT_ALGORITHM', 'HS256')
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv('ACCESS_TOKEN_EXPIRE_MINUTES', '30'))
    REFRESH_TOKEN_EXPIRE_DAYS: int = int(os.getenv('REFRESH_TOKEN_EXPIRE_DAYS', '7'))

    RATE_LIMIT_PER_MINUTE: int = int(os.getenv('RATE_LIMIT_PER_MINUTE', '30'))

    TWILIO_ACCOUNT_SID: str = os.getenv('TWILIO_ACCOUNT_SID', '')
    TWILIO_AUTH_TOKEN: str = os.getenv('TWILIO_AUTH_TOKEN', '')
    TWILIO_WHATSAPP_FROM: str = os.getenv('TWILIO_WHATSAPP_FROM', '')

    RAZORPAY_KEY_ID: str = os.getenv('RAZORPAY_KEY_ID', '')
    RAZORPAY_KEY_SECRET: str = os.getenv('RAZORPAY_KEY_SECRET', '')

    BRIDGE_DEFAULT_TOKEN: str = os.getenv('BRIDGE_DEFAULT_TOKEN', 'bridge-bootstrap-token')

    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'info')


settings = Settings()
