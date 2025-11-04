#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Spilno News Bot - Автоматизація публікації новин
Версія: 1.0
Автор: Claude + Андрій
Дата: 2025-10-27
"""

import os
import sys
import logging
import asyncio
import hashlib
import json
import re
import html
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from io import BytesIO
from urllib.parse import quote

import aiohttp
import asyncpg
from dotenv import load_dotenv
from openai import AsyncOpenAI
from PIL import Image

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BufferedInputFile
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# Завантаження змінних середовища
load_dotenv()

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ============================================
# КОНФІГУРАЦІЯ
# ============================================

class Config:
    """Конфігурація бота"""
    # Telegram
    BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
    ADMIN_IDS = [int(id.strip()) for id in os.getenv('ADMIN_IDS', '').split(',') if id.strip()]

    # OpenAI
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    GPT_MODEL = "gpt-4o-mini"  # або "gpt-4-turbo-preview"

    # GNews
    GNEWS_API_KEY = os.getenv('GNEWS_API_KEY')
    GNEWS_SEARCH_URL = "https://gnews.io/api/v4/search"
    GNEWS_HEADLINES_URL = "https://gnews.io/api/v4/top-headlines"

    # WordPress
    WP_SITE_URL = os.getenv('WP_SITE_URL', 'https://spilno.online').rstrip('/')
    WP_USERNAME = os.getenv('WP_USERNAME')
    WP_APP_PASSWORD = os.getenv('WP_APP_PASSWORD')

    # PostgreSQL
    DATABASE_URL = os.getenv('DATABASE_URL')

    # Валідація
    @classmethod
    def validate(cls):
        """Перевірка наявності всіх необхідних змінних"""
        required = {
            'BOT_TOKEN': cls.BOT_TOKEN,
            'OPENAI_API_KEY': cls.OPENAI_API_KEY,
            'GNEWS_API_KEY': cls.GNEWS_API_KEY,
            'WP_USERNAME': cls.WP_USERNAME,
            'WP_APP_PASSWORD': cls.WP_APP_PASSWORD,
            'DATABASE_URL': cls.DATABASE_URL
        }

        missing = [key for key, value in required.items() if not value]

        if missing:
            logger.error(f"❌ Відсутні змінні середовища: {', '.join(missing)}")
            logger.error("Перевірте файл .env")
            sys.exit(1)

        logger.info("✅ Конфігурація валідна")

# ============================================
# FSM STATES
# ============================================

class ArticleStates(StatesGroup):
    """Стани для створення статті"""
    waiting_for_custom_image = State()
    editing_title = State()
    editing_content = State()
    editing_excerpt = State()
    editing_source_query = State()  # Стан для редагування запиту пошуку джерел
    # Стани для ручного створення статті
    manual_title = State()
    manual_content = State()
    manual_excerpt = State()
    manual_seo = State()
    manual_category = State()
    manual_image = State()

class SearchStates(StatesGroup):
    """Стани для пошуку"""
    waiting_for_operators = State()
    waiting_for_query = State()

# ============================================
# DATABASE
# ============================================

class Database:
    """Робота з PostgreSQL"""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        """Підключення до БД"""
        try:
            self.pool = await asyncpg.create_pool(
                self.database_url,
                min_size=5,
                max_size=20,
                command_timeout=60
            )
            logger.info("✅ З'єднання з БД встановлено")

            # Перевірка з'єднання
            async with self.pool.acquire() as conn:
                version = await conn.fetchval('SELECT version()')
                logger.info(f"PostgreSQL версія: {version.split()[0]} {version.split()[1]}")

        except Exception as e:
            logger.error(f"❌ Помилка підключення до БД: {e}")
            raise

    async def close(self):
        """Закриття з'єднання"""
        if self.pool:
            await self.pool.close()
            logger.info("БД з'єднання закрито")

    # ========== Категорії ==========

    async def get_all_categories(self) -> List[Dict]:
        """Отримати всі категорії"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('SELECT * FROM wp_categories ORDER BY name')
            return [dict(row) for row in rows]

    async def get_category_by_id(self, category_id: int) -> Optional[Dict]:
        """Отримати категорію за ID"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM wp_categories WHERE id = $1',
                category_id
            )
            return dict(row) if row else None

    async def get_category_id_by_name(self, name: str) -> Optional[int]:
        """Отримати ID категорії за назвою"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id FROM wp_categories WHERE name = $1',
                name
            )
            return row['id'] if row else None

    # ========== Чернетки ==========

    async def save_draft(self, user_id: int, title: str, content: str,
                        excerpt: str, category_id: int, seo_description: str,
                        images: list, sources: list) -> int:
        """Зберегти чернетку"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('''
                INSERT INTO drafts (user_id, title, content, excerpt, category_id,
                                   seo_description, images, sources)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
            ''', user_id, title, content, excerpt, category_id,
                seo_description, json.dumps(images), json.dumps(sources))

            return row['id']

    async def get_user_drafts(self, user_id: int) -> List[Dict]:
        """Отримати чернетки користувача"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT d.*, c.name as category_name
                FROM drafts d
                LEFT JOIN wp_categories c ON d.category_id = c.id
                WHERE d.user_id = $1
                ORDER BY d.created_at DESC
            ''', user_id)

            return [dict(row) for row in rows]

    async def get_draft_by_id(self, draft_id: int) -> Optional[Dict]:
        """Отримати чернетку за ID"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('''
                SELECT d.*, c.name as category_name
                FROM drafts d
                LEFT JOIN wp_categories c ON d.category_id = c.id
                WHERE d.id = $1
            ''', draft_id)

            if row:
                result = dict(row)
                result['images'] = json.loads(result['images']) if result['images'] else []
                result['sources'] = json.loads(result['sources']) if result['sources'] else []
                return result
            return None

    async def delete_draft(self, draft_id: int):
        """Видалити чернетку"""
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM drafts WHERE id = $1', draft_id)

    # ========== Опубліковані статті ==========

    async def save_published_article(self, user_id: int, wp_post_id: Optional[int],
                                     tg_message_id: Optional[int], title: str,
                                     url: Optional[str], category_id: int,
                                     published_to_wp: bool, published_to_tg: bool,
                                     sources: list) -> int:
        """Зберегти опубліковану статтю"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('''
                INSERT INTO published_articles
                (user_id, wp_post_id, tg_message_id, title, url, category_id,
                 published_to_wp, published_to_tg)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
            ''', user_id, wp_post_id, tg_message_id, title, url, category_id,
                published_to_wp, published_to_tg)

            return row['id']

    async def get_published_article(self, article_id: int) -> Optional[Dict]:
        """Отримати опубліковану статтю"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('''
                SELECT p.*, c.name as category_name
                FROM published_articles p
                LEFT JOIN wp_categories c ON p.category_id = c.id
                WHERE p.id = $1
            ''', article_id)

            return dict(row) if row else None

    async def get_user_published_articles(self, user_id: int, limit: int = 20) -> List[Dict]:
        """Отримати опубліковані статті користувача"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT p.*, c.name as category_name
                FROM published_articles p
                LEFT JOIN wp_categories c ON p.category_id = c.id
                WHERE p.user_id = $1
                ORDER BY p.published_at DESC
                LIMIT $2
            ''', user_id, limit)

            return [dict(row) for row in rows]

    async def delete_published_article(self, article_id: int):
        """Видалити опубліковану статтю з БД"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'DELETE FROM published_articles WHERE id = $1',
                article_id
            )

    # ========== Fingerprints (перевірка дублів) ==========

    async def add_article_fingerprint(self, title: str):
        """Додати fingerprint статті"""
        title_hash = hashlib.md5(title.lower().encode()).hexdigest()

        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO article_fingerprints (title_hash, original_title)
                VALUES ($1, $2)
                ON CONFLICT (title_hash) DO NOTHING
            ''', title_hash, title)

    async def check_duplicate(self, title: str) -> bool:
        """Перевірити чи є дублікат"""
        title_hash = hashlib.md5(title.lower().encode()).hexdigest()

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT id FROM article_fingerprints WHERE title_hash = $1',
                title_hash
            )
            return row is not None

    # ========== Налаштування користувача ==========

    async def get_user_settings(self, user_id: int) -> Dict:
        """Отримати налаштування користувача"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM user_settings WHERE user_id = $1',
                user_id
            )

            if row:
                result = dict(row)
                result['enabled_categories'] = json.loads(result['enabled_categories'])
                return result
            else:
                # Створити налаштування за замовчуванням
                await conn.execute('''
                    INSERT INTO user_settings (user_id, auto_publish_enabled,
                                             auto_publish_interval, auto_publish_to_wp,
                                             auto_publish_to_tg, enabled_categories)
                    VALUES ($1, FALSE, 180, TRUE, FALSE, '[]')
                ''', user_id)

                return {
                    'user_id': user_id,
                    'auto_publish_enabled': False,
                    'auto_publish_interval': 180,
                    'auto_publish_to_wp': True,
                    'auto_publish_to_tg': False,
                    'enabled_categories': [],
                    'last_publish_time': None
                }

    async def update_user_setting(self, user_id: int, key: str, value):
        """Оновити налаштування користувача"""
        # Ensure settings exist
        await self.get_user_settings(user_id)

        if key == 'enabled_categories':
            value = json.dumps(value)

        async with self.pool.acquire() as conn:
            await conn.execute(
                f'UPDATE user_settings SET {key} = $1 WHERE user_id = $2',
                value, user_id
            )

    async def update_last_publish_time(self, user_id: int):
        """Оновити час останньої публікації"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'UPDATE user_settings SET last_publish_time = NOW() WHERE user_id = $1',
                user_id
            )

    # ========== Логи ==========

    async def log_action(self, user_id: int, action: str, status: str,
                        message: str, details: Optional[Dict] = None):
        """Логування дії"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO logs (user_id, action, status, message, details)
                VALUES ($1, $2, $3, $4, $5)
            ''', user_id, action, status, message,
                json.dumps(details) if details else None)

    async def get_logs(self, user_id: int, log_type: str = 'all',
                      limit: int = 20) -> List[Dict]:
        """Отримати логи"""
        async with self.pool.acquire() as conn:
            if log_type == 'all':
                rows = await conn.fetch('''
                    SELECT * FROM logs
                    WHERE user_id = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                ''', user_id, limit)
            else:
                rows = await conn.fetch('''
                    SELECT * FROM logs
                    WHERE user_id = $1 AND status = $2
                    ORDER BY created_at DESC
                    LIMIT $3
                ''', user_id, log_type, limit)

            return [dict(row) for row in rows]

    async def clear_old_logs(self, user_id: int, days: int = 30) -> int:
        """Очистити старі логи"""
        cutoff_date = datetime.now() - timedelta(days=days)

        async with self.pool.acquire() as conn:
            result = await conn.execute('''
                DELETE FROM logs
                WHERE user_id = $1 AND created_at < $2
            ''', user_id, cutoff_date)

            return int(result.split()[-1])

    # ========== Статистика ==========

    async def get_statistics(self, user_id: int, period: str) -> Dict:
        """Отримати статистику за період"""
        now = datetime.now()

        if period == "today":
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            start_date = now - timedelta(days=7)
        elif period == "month":
            start_date = now - timedelta(days=30)
        elif period == "all":
            start_date = datetime(2020, 1, 1)
        else:
            start_date = now - timedelta(days=7)

        async with self.pool.acquire() as conn:
            # Загальна статистика
            stats = await conn.fetchrow('''
                SELECT
                    COUNT(*) as total_articles,
                    COALESCE(SUM(views), 0) as total_views,
                    COALESCE(SUM(clicks), 0) as total_clicks
                FROM published_articles
                WHERE user_id = $1 AND published_at >= $2
            ''', user_id, start_date)

            # По категоріях
            by_category = await conn.fetch('''
                SELECT c.name, COUNT(*) as count
                FROM published_articles p
                JOIN wp_categories c ON p.category_id = c.id
                WHERE p.user_id = $1 AND p.published_at >= $2
                GROUP BY c.name
                ORDER BY count DESC
            ''', user_id, start_date)

            # Топ стаття
            top_article = await conn.fetchrow('''
                SELECT *
                FROM published_articles
                WHERE user_id = $1 AND published_at >= $2
                ORDER BY views DESC, clicks DESC
                LIMIT 1
            ''', user_id, start_date)

            return {
                'total_articles': stats['total_articles'],
                'total_views': stats['total_views'],
                'total_clicks': stats['total_clicks'],
                'by_category': [dict(row) for row in by_category],
                'top_article': dict(top_article) if top_article else None
            }

# ============================================
# GNEWS SERVICE
# ============================================

class GNewsService:
    """Сервіс для роботи з GNews API"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.search_url = Config.GNEWS_SEARCH_URL
        self.headlines_url = Config.GNEWS_HEADLINES_URL

    def _sanitize_query(self, query: str) -> str:
        """Очистити запит від спеціальних символів для GNews API"""
        if not query or query == "*":
            return "Україна"

        # Видаляємо спеціальні символи, які можуть викликати помилки
        # GNews API не підтримує: , " ' : ; ! ? ( ) [ ] { } < > / \ | @ # $ % ^ & * = + ~
        special_chars = r'[,"\':;!?\(\)\[\]\{\}<>/\\|@#$%^&*=+~]'
        cleaned = re.sub(special_chars, ' ', query)

        # Видаляємо зайві пробіли
        cleaned = ' '.join(cleaned.split())

        # Обмежуємо довжину (GNews має ліміт на довжину запиту)
        if len(cleaned) > 100:
            cleaned = ' '.join(cleaned.split()[:10])

        # Якщо після очищення запит порожній, повертаємо дефолтний
        if not cleaned or len(cleaned.strip()) < 3:
            return "Україна"

        return cleaned.strip()

    async def search_news(self, query: str = "Україна", lang: str = "uk",
                         country: str = "ua", max_results: int = 20) -> List[Dict]:
        """Пошук новин"""
        # Очищаємо запит від спеціальних символів
        cleaned_query = self._sanitize_query(query)

        params = {
            'q': cleaned_query,
            'lang': lang,
            'country': country,
            'max': max_results,
            'apikey': self.api_key
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.search_url, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        articles = data.get('articles', [])
                        logger.info(f"GNews search successful: {len(articles)} articles found for query '{cleaned_query}' (original: '{query}')")
                        return articles
                    else:
                        error_text = await resp.text()
                        logger.error(f"GNews API error {resp.status} for query '{cleaned_query}' (original: '{query}'): {error_text}")
                        return []
        except Exception as e:
            logger.error(f"GNews request failed for query '{cleaned_query}' (original: '{query}'): {e}")
            return []

    async def get_top_headlines(self, category: str = "general", lang: str = "uk",
                               country: str = "ua", max_results: int = 20) -> List[Dict]:
        """Топ новини"""
        params = {
            'category': category,
            'lang': lang,
            'country': country,
            'max': max_results,
            'apikey': self.api_key
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.headlines_url, params=params, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        articles = data.get('articles', [])
                        logger.info(f"GNews headlines successful: {len(articles)} articles found")
                        return articles
                    else:
                        error_text = await resp.text()
                        logger.error(f"GNews API error {resp.status}: {error_text}")
                        return []
        except Exception as e:
            logger.error(f"GNews headlines request failed: {e}")
            return []

# ============================================
# GPT SERVICE
# ============================================

class GPTService:
    """Сервіс для генерації статей через GPT-4"""

    SYSTEM_PROMPT = """Ти — журналіст українського медіа "Спільно".
На основі наданих джерел створи оригінальну статтю українською.

Вимоги:
1. Заголовок: 50-70 символів, SEO-оптимізований
2. Категорія: визнач ОДНУ з [У світі, Вдома, Історії, Наші справи, Поради, Біль]
3. Короткий опис: 120-160 символів
4. Контент: HTML з тегами <h2>, <h3>, <p>, <strong>, <ul>, <li>
   Довжина: 1500-2500 слів
   Стиль: інформативний, доступний
5. SEO-опис: 150-160 символів

ВАЖЛИВО:
- Використовуй ТІЛЬКИ HTML теги (НЕ Markdown)
- НЕ копіюй текст дослівно
- Пиши природньою українською

Формат відповіді (JSON):
{
  "title": "...",
  "category": "У світі",
  "excerpt": "...",
  "content": "<p>...</p>",
  "seo_description": "..."
}"""

    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(api_key=api_key)

    async def generate_article(self, sources: List[Dict]) -> Dict:
        """Генерація статті"""
        sources_text = "\n\n".join([
            f"Джерело {idx+1} ({s.get('source', {}).get('name', 'Unknown')}):\n"
            f"Заголовок: {s.get('title', '')}\n"
            f"Опис: {s.get('description', '')}"
            for idx, s in enumerate(sources[:10])
        ])

        prompt = f"""На основі цих {len(sources)} джерел напиши статтю українською:

{sources_text}

Відповідай ТІЛЬКИ валідним JSON."""

        try:
            response = await self.client.chat.completions.create(
                model=Config.GPT_MODEL,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                response_format={"type": "json_object"}
            )

            article_data = json.loads(response.choices[0].message.content)

            # Валідація
            required = ['title', 'category', 'excerpt', 'content', 'seo_description']
            if not all(field in article_data for field in required):
                raise ValueError("Missing required fields in GPT response")

            return article_data

        except Exception as e:
            logger.error(f"GPT generation failed: {e}")
            raise

    async def translate_to_ukrainian(self, text: str, source_lang: str = "auto") -> str:
        """Перекласти текст на українську мову"""
        try:
            prompt = f"""Переклади наступний текст на українську мову.
Зберігай природність та стиль оригіналу.
Повертай ТІЛЬКИ переклад без додаткових коментарів.

Текст:
{text}"""

            response = await self.client.chat.completions.create(
                model=Config.GPT_MODEL,
                messages=[
                    {"role": "system", "content": "Ти професійний перекладач. Перекладай точно та природньо на українську мову."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=2000
            )

            translation = response.choices[0].message.content.strip()
            return translation

        except Exception as e:
            logger.error(f"Translation failed: {e}")
            return text  # Повертаємо оригінал якщо переклад не вдався

# ============================================
# WORDPRESS SERVICE
# ============================================

class WordPressService:
    """Сервіс для роботи з WordPress REST API"""

    def __init__(self, site_url: str, username: str, app_password: str):
        self.site_url = site_url.rstrip('/')
        self.username = username
        self.app_password = app_password
        self.auth = aiohttp.BasicAuth(username, app_password)

    async def upload_image(self, image_source: Dict, bot: Bot) -> int:
        """Завантажити зображення на WordPress"""
        try:
            # Отримати бінарні дані
            if image_source.get('custom') and image_source.get('file_id'):
                file = await bot.get_file(image_source['file_id'])
                file_bytes = await bot.download_file(file.file_path)
                image_data = file_bytes.read()
                filename = f"article_{int(datetime.now().timestamp())}.jpg"

            elif image_source.get('url'):
                async with aiohttp.ClientSession() as session:
                    async with session.get(image_source['url']) as resp:
                        if resp.status != 200:
                            raise Exception(f"Failed to download: {resp.status}")
                        image_data = await resp.read()

                ext = image_source['url'].split('.')[-1].split('?')[0]
                filename = f"article_{int(datetime.now().timestamp())}.{ext}"
            else:
                raise ValueError("No image source")

            # Оптимізація
            image_data = await self._optimize_image(image_data)

            # Завантаження
            url = f"{self.site_url}/wp-json/wp/v2/media"
            headers = {
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Type': 'image/jpeg'
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=image_data, headers=headers,
                                       auth=self.auth) as resp:
                    if resp.status not in [200, 201]:
                        error = await resp.text()
                        raise Exception(f"WP upload failed: {resp.status} - {error}")

                    result = await resp.json()
                    return result['id']

        except Exception as e:
            logger.error(f"Image upload error: {e}")
            raise

    async def _optimize_image(self, image_data: bytes) -> bytes:
        """Оптимізувати зображення"""
        try:
            img = Image.open(BytesIO(image_data))

            max_width = 1920
            max_height = 1080

            if img.width > max_width or img.height > max_height:
                img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)

            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = background

            output = BytesIO()
            img.save(output, format='JPEG', quality=85, optimize=True)
            return output.getvalue()

        except Exception as e:
            logger.warning(f"Image optimization failed: {e}")
            return image_data

    async def create_post(self, article: Dict, featured_media_id: Optional[int] = None) -> Dict:
        """Створити пост на WordPress"""
        url = f"{self.site_url}/wp-json/wp/v2/posts"

        data = {
            'title': article['title'],
            'content': article['content'],
            'excerpt': article['excerpt'],
            'status': 'publish',
            'categories': [article['category_id']],
            'meta': {
                'seo_description': article.get('seo_description', '')
            }
        }

        if featured_media_id:
            data['featured_media'] = featured_media_id

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, auth=self.auth) as resp:
                if resp.status not in [200, 201]:
                    error = await resp.text()
                    raise Exception(f"Post creation failed: {resp.status} - {error}")

                return await resp.json()

    async def delete_post(self, post_id: int, force: bool = True) -> bool:
        """Видалити пост"""
        url = f"{self.site_url}/wp-json/wp/v2/posts/{post_id}"
        params = {'force': 'true' if force else 'false'}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(url, params=params, auth=self.auth) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.error(f"Delete post error: {e}")
            return False

# ============================================
# SCHEDULER
# ============================================

class AutoPublishScheduler:
    """Планувальник автопублікації"""

    def __init__(self, bot: Bot, db: Database):
        self.bot = bot
        self.db = db
        self.scheduler = AsyncIOScheduler()
        self.gnews = GNewsService(Config.GNEWS_API_KEY)
        self.gpt = GPTService(Config.OPENAI_API_KEY)
        self.wp = WordPressService(Config.WP_SITE_URL, Config.WP_USERNAME, Config.WP_APP_PASSWORD)

    async def auto_publish_task(self, user_id: int):
        """Автоматична публікація"""
        try:
            settings = await self.db.get_user_settings(user_id)

            if not settings['auto_publish_enabled']:
                return

            # Пошук новин
            news = await self.gnews.search_news(query="Україна", lang="uk", max_results=10)

            if not news:
                await self.bot.send_message(user_id, "⚠️ Автопублікація: новини не знайдено")
                return

            main_news = news[0]

            # Пошук джерел - витягуємо ключові слова
            title = main_news.get('title', '')
            query_words = extract_keywords_from_title(title, max_words=5)
            sources = await self.gnews.search_news(query=query_words, max_results=10)

            # Генерація статті
            article = await self.gpt.generate_article(sources)

            # Перевірка категорії
            category_id = await self.db.get_category_id_by_name(article['category'])
            enabled_cats = settings.get('enabled_categories', [])

            if enabled_cats and category_id not in enabled_cats:
                await self.bot.send_message(
                    user_id,
                    f"⏭️ Статтю пропущено (категорія '{article['category']}' вимкнена)"
                )
                return

            # Перевірка дублів
            if await self.db.check_duplicate(article['title']):
                await self.bot.send_message(user_id, "⏭️ Виявлено дублікат статті")
                return

            article['category_id'] = category_id

            # Завантаження зображення
            featured_media_id = None
            if main_news.get('image'):
                try:
                    featured_media_id = await self.wp.upload_image({'url': main_news['image']}, self.bot)
                except Exception as e:
                    logger.warning(f"Image upload failed: {e}")

            # Публікація
            wp_post_id = None
            wp_url = None
            tg_message_id = None

            if settings['auto_publish_to_wp']:
                wp_post = await self.wp.create_post(article, featured_media_id)
                wp_post_id = wp_post['id']
                wp_url = wp_post['link']

            if settings['auto_publish_to_tg']:
                # Конвертуємо HTML для Telegram
                telegram_content = html_to_telegram(article['content'], max_length=3800)

                # Додаємо брендинг та посилання
                if wp_url:
                    telegram_content += f"\n\n📰 <b>Читати повністю:</b> {wp_url}\n\n"
                telegram_content += f"<b>Джерело:</b> <a href='{Config.WP_SITE_URL}'>Спільно</a>"

                tg_msg = await self.bot.send_message(
                    Config.CHANNEL_ID,
                    telegram_content,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )
                tg_message_id = tg_msg.message_id

            # Збереження в БД
            await self.db.save_published_article(
                user_id=user_id,
                wp_post_id=wp_post_id,
                tg_message_id=tg_message_id,
                title=article['title'],
                url=wp_url,
                category_id=category_id,
                published_to_wp=settings['auto_publish_to_wp'],
                published_to_tg=settings['auto_publish_to_tg'],
                sources=sources
            )

            await self.db.add_article_fingerprint(article['title'])
            await self.db.update_last_publish_time(user_id)

            # Повідомлення
            result_text = f"✅ **АВТОПУБЛІКАЦІЯ**\n\n📰 {article['title']}\n\n"
            if wp_url:
                result_text += f"🌐 WordPress: {wp_url}\n"
            if tg_message_id:
                result_text += f"📱 Telegram: опубліковано\n"

            await self.bot.send_message(user_id, result_text, parse_mode="Markdown")

            await self.db.log_action(user_id, 'auto_publish', 'success',
                                     f"Auto-published: {article['title']}",
                                     {'wp_post_id': wp_post_id, 'url': wp_url})

        except Exception as e:
            logger.error(f"Auto-publish failed: {e}")
            await self.db.log_action(user_id, 'auto_publish', 'error', str(e), {})
            await self.bot.send_message(user_id, f"❌ Помилка автопублікації: {str(e)}")

    def start_user_schedule(self, user_id: int, interval_minutes: int):
        """Запустити розклад для користувача"""
        job_id = f"auto_publish_{user_id}"

        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        self.scheduler.add_job(
            self.auto_publish_task,
            trigger=IntervalTrigger(minutes=interval_minutes),
            args=[user_id],
            id=job_id,
            replace_existing=True
        )

        logger.info(f"Started schedule for user {user_id}: {interval_minutes}min")

    def stop_user_schedule(self, user_id: int):
        """Зупинити розклад"""
        job_id = f"auto_publish_{user_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
            logger.info(f"Stopped schedule for user {user_id}")

    def start(self):
        """Запустити планувальник"""
        self.scheduler.start()
        logger.info("Scheduler started")

    def shutdown(self):
        """Зупинити планувальник"""
        self.scheduler.shutdown()
        logger.info("Scheduler stopped")

# ============================================
# TELEGRAM BOT HANDLERS
# ============================================

router = Router()

# Утиліти
def strip_html_tags(text: str) -> str:
    """Видалити HTML теги"""
    return re.sub(r'<[^>]+>', '', text)

def extract_keywords_from_title(title: str, max_words: int = 5) -> str:
    """
    Витягнути ключові слова з заголовка для пошуку
    Видаляє спеціальні символи та бере перші значущі слова
    """
    if not title:
        return "Україна"

    # Видаляємо спеціальні символи
    cleaned = re.sub(r'[,"\':;!?\(\)\[\]\{\}<>/\\|@#$%^&*=+~]', ' ', title)

    # Розбиваємо на слова та фільтруємо короткі слова (часто це прийменники)
    words = [w for w in cleaned.split() if len(w) > 2]

    # Беремо перші max_words слів
    keywords = ' '.join(words[:max_words])

    # Якщо після фільтрації нічого не залишилось
    if not keywords or len(keywords.strip()) < 3:
        return "Україна"

    return keywords.strip()

def html_to_telegram(html_content: str, max_length: int = 4000) -> str:
    """
    Конвертувати HTML в формат, підтримуваний Telegram
    Telegram підтримує тільки: <b>, <i>, <u>, <s>, <code>, <pre>, <a>
    """
    # Конвертуємо заголовки в жирний текст з новими рядками
    html_content = re.sub(r'<h[1-6][^>]*>(.*?)</h[1-6]>', r'\n\n<b>\1</b>\n\n', html_content, flags=re.DOTALL)

    # Конвертуємо <strong> і <b> (залишаємо як є)
    html_content = re.sub(r'<strong>(.*?)</strong>', r'<b>\1</b>', html_content, flags=re.DOTALL)

    # Конвертуємо <em> в <i>
    html_content = re.sub(r'<em>(.*?)</em>', r'<i>\1</i>', html_content, flags=re.DOTALL)

    # Видаляємо <ul>, <ol> теги, залишаючи вміст
    html_content = re.sub(r'</?ul[^>]*>', '', html_content)
    html_content = re.sub(r'</?ol[^>]*>', '', html_content)

    # Конвертуємо <li> в • (bullet points)
    html_content = re.sub(r'<li[^>]*>(.*?)</li>', r'\n• \1', html_content, flags=re.DOTALL)

    # Конвертуємо <p> в новий рядок
    html_content = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n\n', html_content, flags=re.DOTALL)

    # Видаляємо <br>, <br/>, <br /> і замінюємо на \n
    html_content = re.sub(r'<br\s*/?>', '\n', html_content)

    # Видаляємо всі інші теги (окрім дозволених Telegram)
    allowed_tags = r'(</?(?:b|i|u|s|code|pre|a)[^>]*>)'
    html_content = re.sub(r'<(?!/?(b|i|u|s|code|pre|a))[^>]+>', '', html_content)

    # Очищаємо зайві пробіли та порожні рядки
    html_content = re.sub(r'\n{3,}', '\n\n', html_content)
    html_content = html_content.strip()

    # Обрізаємо до максимальної довжини
    if len(html_content) > max_length:
        html_content = html_content[:max_length-3] + "..."

    return html_content

# ========== Команди ==========

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, db: Database):
    """Команда /start"""
    await state.clear()

    text = f"""👋 **Вітаю, {message.from_user.first_name}!**

Я бот для автоматичної публікації новин на **spilno.online** та в Telegram канал.

**Можливості:**
📰 Пошук актуальних новин
✍️ Генерація статей через GPT-4
🖼️ Робота із зображеннями
🤖 Автопублікація за розкладом
📊 Статистика та аналітика

Почнімо роботу!"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📰 Нові новини", callback_data="fetch_news")],
        [InlineKeyboardButton(text="✍️ Створити статтю вручну", callback_data="create_manual_article")],
        [
            InlineKeyboardButton(text="📝 Чернетки", callback_data="show_drafts"),
            InlineKeyboardButton(text="🌐 Опубліковані", callback_data="show_published")
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="statistics"),
            InlineKeyboardButton(text="⚙️ Налаштування", callback_data="settings")
        ]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

# ========== Головне меню ==========

@router.callback_query(F.data == "main_menu")
async def show_main_menu(callback: CallbackQuery, db: Database):
    """Головне меню"""
    user_id = callback.from_user.id
    stats = await db.get_statistics(user_id, 'today')
    settings = await db.get_user_settings(user_id)

    auto_status = "🟢" if settings['auto_publish_enabled'] else "⚪"

    text = f"""🏠 **ГОЛОВНЕ МЕНЮ**

📊 **Сьогодні:**
📰 Опубліковано: {stats['total_articles']} статей
👁️ Переглядів: {stats['total_views']:,}

🤖 **Автопублікація:** {auto_status}

Оберіть дію:"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📰 Нові новини", callback_data="fetch_news")],
        [InlineKeyboardButton(text="🔍 Пошук новин", callback_data="search_news")],
        [InlineKeyboardButton(text="✍️ Створити статтю вручну", callback_data="create_manual_article")],
        [
            InlineKeyboardButton(text="📝 Чернетки", callback_data="show_drafts"),
            InlineKeyboardButton(text="🌐 Опубліковані", callback_data="show_published")
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="statistics"),
            InlineKeyboardButton(text="⚙️ Налаштування", callback_data="settings")
        ]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

# ========== Ручне створення статті ==========

@router.callback_query(F.data == "create_manual_article")
async def create_manual_article_handler(callback: CallbackQuery, state: FSMContext):
    """Початок створення статті вручну"""
    await state.clear()

    text = """✍️ **СТВОРЕННЯ СТАТТІ ВРУЧНУ**

Ви можете створити і опублікувати статтю без використання AI.

Почнемо з заголовка. Надішліть заголовок статті:

💡 **Порада:** Заголовок повинен бути коротким та привабливим (50-70 символів)"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="main_menu")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(ArticleStates.manual_title)

@router.message(ArticleStates.manual_title)
async def process_manual_title(message: Message, state: FSMContext):
    """Обробка заголовка"""
    title = message.text.strip()

    if len(title) < 10:
        await message.answer("❌ Заголовок занадто короткий. Спробуйте ще раз (мінімум 10 символів):")
        return

    await state.update_data(manual_article={'title': title})

    text = f"""✅ Заголовок збережено!

📰 **{title}**

Тепер надішліть **основний контент** статті.

💡 **Підказка:**
- Використовуйте HTML теги для форматування: <b>жирний</b>, <i>курсив</i>, <p>параграф</p>
- Або просто напишіть звичайний текст
- Мінімум 200 символів"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="main_menu")]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(ArticleStates.manual_content)

@router.message(ArticleStates.manual_content)
async def process_manual_content(message: Message, state: FSMContext):
    """Обробка контенту"""
    content = message.text.strip()

    if len(content) < 200:
        await message.answer("❌ Контент занадто короткий. Спробуйте ще раз (мінімум 200 символів):")
        return

    # Обгортаємо в параграфи якщо немає HTML
    if not re.search(r'<[^>]+>', content):
        paragraphs = content.split('\n\n')
        content = '\n'.join([f'<p>{p}</p>' for p in paragraphs if p.strip()])

    data = await state.get_data()
    article = data.get('manual_article', {})
    article['content'] = content
    await state.update_data(manual_article=article)

    text = f"""✅ Контент збережено! ({len(content)} символів)

Тепер надішліть **короткий опис** статті для анонсу.

💡 **Підказка:** 120-160 символів, стисло про що стаття"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="main_menu")]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(ArticleStates.manual_excerpt)

@router.message(ArticleStates.manual_excerpt)
async def process_manual_excerpt(message: Message, state: FSMContext):
    """Обробка опису"""
    excerpt = message.text.strip()

    if len(excerpt) < 50:
        await message.answer("❌ Опис занадто короткий. Спробуйте ще раз (мінімум 50 символів):")
        return

    data = await state.get_data()
    article = data.get('manual_article', {})
    article['excerpt'] = excerpt
    await state.update_data(manual_article=article)

    text = f"""✅ Опис збережено!

Тепер надішліть **SEO опис** для пошукових систем.

💡 **Підказка:** 150-160 символів, включіть ключові слова"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ Пропустити", callback_data="skip_manual_seo")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="main_menu")]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(ArticleStates.manual_seo)

@router.callback_query(F.data == "skip_manual_seo")
async def skip_manual_seo_handler(callback: CallbackQuery, state: FSMContext, db: Database):
    """Пропустити SEO опис"""
    data = await state.get_data()
    article = data.get('manual_article', {})
    article['seo_description'] = article.get('excerpt', '')[:160]
    await state.update_data(manual_article=article)

    await select_manual_category_handler(callback, state, db)

@router.message(ArticleStates.manual_seo)
async def process_manual_seo(message: Message, state: FSMContext, db: Database):
    """Обробка SEO опису"""
    seo_desc = message.text.strip()

    data = await state.get_data()
    article = data.get('manual_article', {})
    article['seo_description'] = seo_desc
    await state.update_data(manual_article=article)

    # Переходимо до вибору категорії
    text = "✅ SEO опис збережено!\n\nТепер оберіть категорію:"

    categories = await db.get_all_categories()
    keyboard = []
    for cat in categories:
        keyboard.append([InlineKeyboardButton(
            text=f"📁 {cat['name']}",
            callback_data=f"manual_cat:{cat['id']}"
        )])
    keyboard.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="main_menu")])

    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")
    await state.set_state(ArticleStates.manual_category)

async def select_manual_category_handler(callback: CallbackQuery, state: FSMContext, db: Database):
    """Вибір категорії для ручної статті"""
    text = "📁 **КАТЕГОРІЯ**\n\nОберіть категорію для статті:"

    categories = await db.get_all_categories()
    keyboard = []
    for cat in categories:
        keyboard.append([InlineKeyboardButton(
            text=f"📁 {cat['name']}",
            callback_data=f"manual_cat:{cat['id']}"
        )])
    keyboard.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="main_menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")
    await state.set_state(ArticleStates.manual_category)

@router.callback_query(F.data.startswith("manual_cat:"))
async def process_manual_category(callback: CallbackQuery, state: FSMContext, db: Database):
    """Обробка вибору категорії"""
    category_id = int(callback.data.split(":")[1])
    category = await db.get_category_by_id(category_id)

    data = await state.get_data()
    article = data.get('manual_article', {})
    article['category_id'] = category_id
    article['category'] = category['name']
    await state.update_data(manual_article=article)

    text = f"""✅ Категорія: **{category['name']}**

📸 **ЗОБРАЖЕННЯ**

Хочете додати зображення до статті?"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Завантажити зображення", callback_data="upload_manual_image")],
        [InlineKeyboardButton(text="⏭️ Без зображення", callback_data="skip_manual_image")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="main_menu")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "upload_manual_image")
async def upload_manual_image_handler(callback: CallbackQuery, state: FSMContext):
    """Запит на завантаження зображення"""
    text = """📸 **ЗАВАНТАЖЕННЯ ЗОБРАЖЕННЯ**

Надішліть зображення для статті (фото):"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ Пропустити", callback_data="skip_manual_image")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="main_menu")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(ArticleStates.manual_image)

@router.message(ArticleStates.manual_image, F.photo)
async def process_manual_image(message: Message, state: FSMContext):
    """Обробка зображення"""
    photo = message.photo[-1]  # Найбільший розмір

    data = await state.get_data()
    article = data.get('manual_article', {})
    article['image'] = {'file_id': photo.file_id, 'custom': True}
    await state.update_data(manual_article=article)

    await show_manual_article_preview(message, state)

@router.callback_query(F.data == "skip_manual_image")
async def skip_manual_image_handler(callback: CallbackQuery, state: FSMContext):
    """Пропустити зображення"""
    data = await state.get_data()
    article = data.get('manual_article', {})
    article['image'] = None
    await state.update_data(manual_article=article)

    await show_manual_article_preview(callback.message, state, is_callback=True)

async def show_manual_article_preview(message_or_callback, state: FSMContext, is_callback: bool = False):
    """Показати превью статті"""
    data = await state.get_data()
    article = data.get('manual_article', {})

    content_preview = strip_html_tags(article['content'])[:300]

    text = f"""✅ **СТАТТЮ СТВОРЕНО!**

📰 **Заголовок:**
{article['title']}

📁 **Категорія:** {article.get('category', 'N/A')}

📝 **Опис:**
{article['excerpt']}

📄 **Превью:**
{content_preview}...

🔍 **SEO:** {article.get('seo_description', 'N/A')}

📸 **Зображення:** {'✅ Є' if article.get('image') else '❌ Немає'}

Що робити далі?"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Опублікувати", callback_data="publish_manual_article")],
        [InlineKeyboardButton(text="💾 Зберегти як чернетку", callback_data="save_manual_draft")],
        [InlineKeyboardButton(text="🔙 Скасувати", callback_data="main_menu")]
    ])

    if is_callback:
        await message_or_callback.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message_or_callback.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "publish_manual_article")
async def publish_manual_article_handler(callback: CallbackQuery, state: FSMContext):
    """Меню публікації ручної статті"""
    text = "📱 **КУДИ ОПУБЛІКУВАТИ?**\n\nОберіть один або обидва варіанти:"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 WordPress (spilno.online)", callback_data="publish_manual_wp")],
        [InlineKeyboardButton(text="📱 Telegram канал", callback_data="publish_manual_tg")],
        [InlineKeyboardButton(text="🚀 Обидва", callback_data="publish_manual_both")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="create_manual_article")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data.startswith("publish_manual_"))
async def process_manual_publish(callback: CallbackQuery, state: FSMContext, db: Database):
    """Публікація ручної статті"""
    publish_type = callback.data.split("_")[-1]  # wp, tg, both

    data = await state.get_data()
    article = data.get('manual_article', {})

    progress_msg = await callback.message.edit_text("📤 Публікую...")

    try:
        wp_service = WordPressService(Config.WP_SITE_URL, Config.WP_USERNAME, Config.WP_APP_PASSWORD)

        wp_post_id = None
        wp_url = None
        tg_message_id = None
        featured_media_id = None

        # WordPress
        if publish_type in ['wp', 'both']:
            await progress_msg.edit_text("📤 Завантажую зображення...")

            if article.get('image'):
                try:
                    featured_media_id = await wp_service.upload_image(article['image'], callback.bot)
                except Exception as e:
                    logger.warning(f"Image upload failed: {e}")

            await progress_msg.edit_text("📤 Створюю пост на WordPress...")
            wp_post = await wp_service.create_post(article, featured_media_id)
            wp_post_id = wp_post['id']
            wp_url = wp_post['link']

        # Telegram
        if publish_type in ['tg', 'both']:
            await progress_msg.edit_text("📤 Публікую в Telegram...")
            telegram_content = html_to_telegram(article['content'], max_length=3800)

            if wp_url:
                telegram_content += f"\n\n📰 <b>Читати повністю:</b> {wp_url}\n\n"
            telegram_content += f"<b>Джерело:</b> <a href='{Config.WP_SITE_URL}'>Спільно</a>"

            tg_msg = await callback.bot.send_message(
                Config.CHANNEL_ID,
                telegram_content,
                parse_mode="HTML",
                disable_web_page_preview=False
            )
            tg_message_id = tg_msg.message_id

        # Збереження
        await progress_msg.edit_text("📤 Зберігаю в БД...")
        await db.save_published_article(
            user_id=callback.from_user.id,
            wp_post_id=wp_post_id,
            tg_message_id=tg_message_id,
            title=article['title'],
            url=wp_url,
            category_id=article['category_id'],
            published_to_wp=(publish_type in ['wp', 'both']),
            published_to_tg=(publish_type in ['tg', 'both']),
            sources=[]
        )

        await db.add_article_fingerprint(article['title'])
        await db.log_action(callback.from_user.id, 'publish_manual', 'success',
                           f"Published manual: {article['title']}", {'wp_post_id': wp_post_id})

        # Результат
        result_text = f"✅ **ОПУБЛІКОВАНО!**\n\n📰 **{article['title']}**\n\n"
        if wp_url:
            result_text += f"🌐 WordPress: {wp_url}\n"
        if tg_message_id:
            result_text += f"📱 Telegram: опубліковано\n"
        result_text += f"\n📁 Категорія: {article['category']}"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")]
        ])

        await progress_msg.edit_text(result_text, reply_markup=keyboard, parse_mode="Markdown")
        await state.clear()

    except Exception as e:
        logger.error(f"Manual publishing failed: {e}")
        await db.log_action(callback.from_user.id, 'publish_manual', 'error', str(e), {})

        await progress_msg.edit_text(
            f"❌ Помилка публікації:\n\n{str(e)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Спробувати ще", callback_data="publish_manual_article")],
                [InlineKeyboardButton(text="🔙 Головне меню", callback_data="main_menu")]
            ])
        )

@router.callback_query(F.data == "save_manual_draft")
async def save_manual_draft_handler(callback: CallbackQuery, state: FSMContext, db: Database):
    """Зберегти ручну статтю як чернетку"""
    data = await state.get_data()
    article = data.get('manual_article', {})

    try:
        draft_id = await db.save_draft(
            user_id=callback.from_user.id,
            title=article['title'],
            content=article['content'],
            excerpt=article['excerpt'],
            category_id=article['category_id'],
            seo_description=article.get('seo_description', ''),
            images=[article['image']] if article.get('image') else [],
            sources=[]
        )

        await callback.message.edit_text(
            f"💾 **Чернетку збережено!**\n\nID: {draft_id}\n\n"
            f"Знайти в: 📝 Чернетки",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        await state.clear()

    except Exception as e:
        await callback.answer(f"❌ Помилка: {str(e)}", show_alert=True)

# ========== Новини ==========

@router.callback_query(F.data == "fetch_news")
async def fetch_news_handler(callback: CallbackQuery, state: FSMContext):
    """Показати меню вибору геолокації для новин"""
    text = "🌍 **ВИБІР РЕГІОНУ**\n\nОберіть регіон для завантаження новин:"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇦 Україна", callback_data="news_geo:ua")],
        [InlineKeyboardButton(text="🇺🇸 США", callback_data="news_geo:us")],
        [InlineKeyboardButton(text="🇨🇳 Китай", callback_data="news_geo:cn")],
        [InlineKeyboardButton(text="🇷🇺 Росія", callback_data="news_geo:ru")],
        [InlineKeyboardButton(text="🇪🇺 Європа", callback_data="news_geo:eu")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data.startswith("news_geo:"))
async def fetch_news_by_geo_handler(callback: CallbackQuery, state: FSMContext):
    """Завантажити новини за вибраною геолокацією"""
    geo_code = callback.data.split(":")[1]

    # Мапінг кодів на параметри для GNews API
    geo_config = {
        'ua': {'country': 'ua', 'lang': 'uk', 'name': 'Україна', 'flag': '🇺🇦'},
        'us': {'country': 'us', 'lang': 'en', 'name': 'США', 'flag': '🇺🇸'},
        'cn': {'country': 'cn', 'lang': 'zh', 'name': 'Китай', 'flag': '🇨🇳'},
        'ru': {'country': 'ru', 'lang': 'ru', 'name': 'Росія', 'flag': '🇷🇺'},
        'eu': {'country': 'de', 'lang': 'de', 'name': 'Європа', 'flag': '🇪🇺'},  # Використовуємо Німеччину як представника ЄС
    }

    config = geo_config.get(geo_code, geo_config['ua'])

    await callback.message.edit_text(f"⏳ Завантажую новини з регіону {config['flag']} {config['name']}...")

    gnews = GNewsService(Config.GNEWS_API_KEY)
    news = await gnews.get_top_headlines(
        lang=config['lang'],
        country=config['country'],
        max_results=20
    )

    if not news:
        await callback.message.edit_text(
            f"❌ Не вдалося завантажити новини для регіону {config['name']}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="fetch_news")]
            ])
        )
        return

    # Перекладаємо заголовки одразу, якщо мова не українська
    if config['lang'] != 'uk':
        await callback.message.edit_text(f"🔄 Перекладаю новини на українську...")
        gpt = GPTService(Config.OPENAI_API_KEY)

        for article in news[:10]:  # Перекладаємо тільки перші 10 новин які будуть показані
            try:
                title = article.get('title', '')
                description = article.get('description', '')

                # Перекладаємо заголовок та опис
                title_translated = await gpt.translate_to_ukrainian(title)
                description_translated = await gpt.translate_to_ukrainian(description)

                # Зберігаємо переклади
                article['title_uk'] = title_translated
                article['description_uk'] = description_translated
            except Exception as e:
                logger.error(f"Translation error: {e}")
                article['title_uk'] = article.get('title', '')
                article['description_uk'] = article.get('description', '')
    else:
        # Для українських новин просто копіюємо оригінальні заголовки
        for article in news[:10]:
            article['title_uk'] = article.get('title', '')
            article['description_uk'] = article.get('description', '')

    # Збереження в стан
    await state.update_data(news_list=news, selected_geo=config)

    text = f"📰 **НОВІ НОВИНИ {config['flag']} {config['name'].upper()}** (останні {len(news)})\n\nОберіть новину:"

    keyboard = []
    for i, article in enumerate(news[:10], 1):
        # Використовуємо перекладений заголовок
        title_uk = article.get('title_uk', article.get('title', 'No title'))[:50]
        keyboard.append([InlineKeyboardButton(
            text=f"{i}️⃣ {title_uk}...",
            callback_data=f"select_news:{i-1}"
        )])

    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data.startswith("select_news:"))
async def select_news_handler(callback: CallbackQuery, state: FSMContext):
    """Вибрати новину"""
    index = int(callback.data.split(":")[1])
    data = await state.get_data()
    news_list = data.get('news_list', [])
    selected_geo = data.get('selected_geo', {})

    if index >= len(news_list):
        await callback.answer("❌ Новину не знайдено")
        return

    article = news_list[index]

    await state.update_data(selected_article=article)

    # Використовуємо вже перекладені заголовки
    display_title = article.get('title_uk', article.get('title', ''))
    display_description = article.get('description_uk', article.get('description', ''))

    # Показуємо оригінал якщо це не українська
    original_text = ""
    if selected_geo.get('lang') != 'uk':
        original_text = f"\n\n🌐 **Оригінал ({selected_geo.get('name', '')}):**\n_{article.get('title', '')[:100]}..._\n"

    text = f"""📰 **{display_title}**

📝 {display_description[:300]}...{original_text}

🕐 {article.get('publishedAt', '')}
📰 {article.get('source', {}).get('name', 'Unknown')}

Що робити далі?"""

    # Додаємо кнопку переходу до першоджерела
    article_url = article.get('url', '')

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Перейти до першоджерела", url=article_url)] if article_url else [],
        [InlineKeyboardButton(text="🔍 Знайти джерела", callback_data=f"find_sources:{index}")],
        [InlineKeyboardButton(text="✍️ Написати статтю", callback_data=f"write_article:{index}")],
        [InlineKeyboardButton(text="🔙 Назад до списку", callback_data="back_to_news_list")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "back_to_news_list")
async def back_to_news_list_handler(callback: CallbackQuery, state: FSMContext):
    """Повернутися до списку новин"""
    data = await state.get_data()
    news_list = data.get('news_list', [])
    selected_geo = data.get('selected_geo', {})

    if not news_list or not selected_geo:
        # Якщо немає збережених новин, повертаємось до вибору гео
        await fetch_news_handler(callback, state)
        return

    text = f"📰 **НОВІ НОВИНИ {selected_geo['flag']} {selected_geo['name'].upper()}** (останні {len(news_list)})\n\nОберіть новину:"

    keyboard = []
    for i, article in enumerate(news_list[:10], 1):
        title_uk = article.get('title_uk', article.get('title', 'No title'))[:50]
        keyboard.append([InlineKeyboardButton(
            text=f"{i}️⃣ {title_uk}...",
            callback_data=f"select_news:{i-1}"
        )])

    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data.startswith("write_article:"))
async def write_article_direct_handler(callback: CallbackQuery, state: FSMContext, db: Database):
    """Написати статтю одразу з новини"""
    await callback.message.edit_text("🔍 Шукаю джерела та генерую статтю...")

    data = await state.get_data()
    article = data.get('selected_article', {})

    gnews = GNewsService(Config.GNEWS_API_KEY)
    # Витягуємо ключові слова з заголовку
    title = article.get('title', '')
    query_words = extract_keywords_from_title(title, max_words=5)

    sources = await gnews.search_news(query=query_words, max_results=15)

    if not sources:
        await callback.message.edit_text(
            "❌ Джерела не знайдено. Спробуйте іншу новину.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="fetch_news")]
            ])
        )
        return

    await state.update_data(sources=sources)
    # Викликаємо функцію генерації статті
    await write_article_handler(callback, state, db)

@router.callback_query(F.data.startswith("find_sources:"))
async def find_sources_handler(callback: CallbackQuery, state: FSMContext):
    """Знайти джерела"""
    data = await state.get_data()
    article = data.get('selected_article', {})
    title = article.get('title_uk', article.get('title', ''))

    # Витягуємо ключові слова з перекладеного заголовка
    query_words = extract_keywords_from_title(title, max_words=5)

    # Зберігаємо запит та контекст для можливості редагування
    await state.update_data(current_search_query=query_words, search_context='geo')

    await callback.message.edit_text(f"🔍 Шукаю джерела за запитом: *{query_words}*...", parse_mode="Markdown")

    gnews = GNewsService(Config.GNEWS_API_KEY)
    sources = await gnews.search_news(query=query_words, max_results=15)

    if not sources:
        await callback.message.edit_text(
            f"❌ Джерела не знайдено за запитом: *{query_words}*\n\n"
            f"Спробуйте змінити запит вручну або оберіть іншу дію.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Змінити запит", callback_data="edit_source_query")],
                [InlineKeyboardButton(text="🔙 Назад до списку", callback_data="back_to_news_list")]
            ]),
            parse_mode="Markdown"
        )
        return

    await state.update_data(sources=sources)

    text = f"🔍 **Знайдено {len(sources)} джерел:**\n\n"
    text += f"_Запит: {query_words}_\n\n"

    for i, src in enumerate(sources[:10], 1):
        text += f"{i}️⃣ **{src.get('source', {}).get('name', 'Unknown')}**\n"
        text += f"_{src.get('title', '')[:60]}..._\n"
        if src.get('image'):
            text += "🖼️ Є зображення\n"
        text += "\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Змінити запит", callback_data="edit_source_query")],
        [InlineKeyboardButton(text="🖼️ Показати зображення", callback_data="show_images")],
        [InlineKeyboardButton(text="✍️ Написати статтю", callback_data="write_from_sources")],
        [InlineKeyboardButton(text="🔙 Назад до списку", callback_data="back_to_news_list")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "show_images")
async def show_images_handler(callback: CallbackQuery, state: FSMContext):
    """Показати зображення"""
    data = await state.get_data()
    sources = data.get('sources', [])

    images = [s for s in sources if s.get('image')]

    if not images:
        await callback.answer("❌ Зображення не знайдено", show_alert=True)
        return

    await callback.message.answer(f"🖼️ **Доступно {len(images)} зображень:**")

    for i, img in enumerate(images[:5], 1):
        try:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Вибрати", callback_data=f"select_image:{i-1}")]
            ])

            await callback.message.answer_photo(
                photo=img['image'],
                caption=f"**Джерело:** {img.get('source', {}).get('name', 'Unknown')}",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            await asyncio.sleep(0.3)
        except:
            continue

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Завантажити своє", callback_data="upload_custom_image")],
        [InlineKeyboardButton(text="❌ Без зображення", callback_data="no_image")]
    ])

    await callback.message.answer("Або:", reply_markup=keyboard)

@router.callback_query(F.data.startswith("select_image:"))
async def select_image_handler(callback: CallbackQuery, state: FSMContext):
    """Вибрати зображення"""
    index = int(callback.data.split(":")[1])
    data = await state.get_data()
    sources = data.get('sources', [])
    images = [s for s in sources if s.get('image')]

    if index < len(images):
        selected = {'url': images[index]['image'], 'source': images[index].get('source', {}).get('name')}
        await state.update_data(selected_image=selected)
        await callback.answer("✅ Зображення вибрано")

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Написати статтю", callback_data="write_from_sources")]
        ])

        await callback.message.answer("✅ Зображення вибрано! Тепер можна писати статтю.", reply_markup=keyboard)

@router.callback_query(F.data == "no_image")
async def no_image_handler(callback: CallbackQuery, state: FSMContext):
    """Без зображення"""
    await state.update_data(selected_image=None)
    await callback.answer("✅ Продовжуємо без зображення")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написати статтю", callback_data="write_from_sources")]
    ])

    await callback.message.answer("Готово до створення статті", reply_markup=keyboard)

# ========== Пошук новин ==========

@router.callback_query(F.data == "search_news")
async def search_news_handler(callback: CallbackQuery, state: FSMContext):
    """Початок пошуку новин"""
    await state.clear()

    text = """🔍 **ПОШУК НОВИН**

Введіть тему або ключові слова для пошуку новин.

💡 **Приклади:**
- Україна технології
- Штучний інтелект
- Економіка Європи
- Зміна клімату

Новини будуть автоматично перекладені на українську мову."""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(SearchStates.waiting_for_query)

@router.message(SearchStates.waiting_for_query)
async def process_search_query(message: Message, state: FSMContext):
    """Обробка пошукового запиту"""
    query = message.text.strip()

    if len(query) < 2:
        await message.answer("❌ Запит занадто короткий. Спробуйте ще раз (мінімум 2 символи):")
        return

    await message.answer(f"🔍 Шукаю новини за запитом: *{query}*...", parse_mode="Markdown")

    gnews = GNewsService(Config.GNEWS_API_KEY)
    news = await gnews.search_news(query=query, lang="uk", max_results=20)

    if not news:
        # Спробуємо пошук без обмеження по мові
        news = await gnews.search_news(query=query, max_results=20)

    if not news:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Новий пошук", callback_data="search_news")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
        ])

        await message.answer(
            f"❌ Новини не знайдено за запитом: *{query}*\n\n"
            f"Спробуйте інший запит або інші ключові слова.",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        await state.clear()
        return

    # Перекладаємо новини на українську якщо потрібно
    await message.answer("🔄 Перекладаю новини на українську...")
    gpt = GPTService(Config.OPENAI_API_KEY)

    for article in news[:15]:  # Перекладаємо перші 15 новин
        try:
            title = article.get('title', '')
            description = article.get('description', '')

            # Перекладаємо тільки якщо текст не схожий на українську
            # Простий хак: якщо в тексті є кирилиця але немає специфічних українських літер
            needs_translation = True
            if any(char in title for char in ['і', 'є', 'ї', 'ґ', 'И', 'Е', 'Ї', 'Ґ']):
                # Схоже на українську, перекладати не потрібно
                needs_translation = False

            if needs_translation:
                title_translated = await gpt.translate_to_ukrainian(title)
                description_translated = await gpt.translate_to_ukrainian(description)
                article['title_uk'] = title_translated
                article['description_uk'] = description_translated
            else:
                article['title_uk'] = title
                article['description_uk'] = description

        except Exception as e:
            logger.error(f"Translation error: {e}")
            article['title_uk'] = article.get('title', '')
            article['description_uk'] = article.get('description', '')

    # Збереження в стан
    await state.update_data(search_results=news, search_query=query)

    # Групуємо новини по джерелах
    sources_dict = {}
    for article in news[:15]:
        source_name = article.get('source', {}).get('name', 'Unknown')
        if source_name not in sources_dict:
            sources_dict[source_name] = []
        sources_dict[source_name].append(article)

    text = f"🔍 **РЕЗУЛЬТАТИ ПОШУКУ**\n\nЗапит: *{query}*\n\n"
    text += f"Знайдено **{len(news)}** новин з **{len(sources_dict)}** джерел:\n\n"

    # Показуємо джерела
    for source_name, articles in list(sources_dict.items())[:10]:
        text += f"📰 **{source_name}** ({len(articles)} новин)\n"

    text += f"\n💡 Оберіть дію:"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Показати всі новини", callback_data="show_search_results")],
        [InlineKeyboardButton(text="📰 Показати по джерелах", callback_data="show_by_sources")],
        [InlineKeyboardButton(text="🔄 Новий пошук", callback_data="search_news")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    # НЕ очищаємо state, щоб кнопки працювали!

@router.callback_query(F.data == "show_search_results")
async def show_search_results_handler(callback: CallbackQuery, state: FSMContext):
    """Показати результати пошуку списком"""
    data = await state.get_data()
    news = data.get('search_results', [])
    query = data.get('search_query', '')

    if not news:
        await callback.answer("❌ Результати пошуку не знайдено", show_alert=True)
        return

    text = f"📋 **РЕЗУЛЬТАТИ ПОШУКУ**\n\nЗапит: *{query}*\n\nОберіть новину:"

    keyboard = []
    for i, article in enumerate(news[:15], 1):
        title_uk = article.get('title_uk', article.get('title', 'No title'))[:50]
        source_name = article.get('source', {}).get('name', 'Unknown')[:15]
        keyboard.append([InlineKeyboardButton(
            text=f"{i}. {title_uk}... [{source_name}]",
            callback_data=f"select_search_result:{i-1}"
        )])

    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data == "show_by_sources")
async def show_by_sources_handler(callback: CallbackQuery, state: FSMContext):
    """Показати новини згруповані по джерелах"""
    data = await state.get_data()
    news = data.get('search_results', [])
    query = data.get('search_query', '')

    if not news:
        await callback.answer("❌ Результати пошуку не знайдено", show_alert=True)
        return

    # Групуємо по джерелах
    sources_dict = {}
    for article in news[:15]:
        source_name = article.get('source', {}).get('name', 'Unknown')
        if source_name not in sources_dict:
            sources_dict[source_name] = []
        sources_dict[source_name].append(article)

    text = f"📰 **НОВИНИ ПО ДЖЕРЕЛАХ**\n\nЗапит: *{query}*\n\nОберіть джерело:"

    keyboard = []
    for i, (source_name, articles) in enumerate(list(sources_dict.items())[:10], 1):
        keyboard.append([InlineKeyboardButton(
            text=f"{source_name} ({len(articles)} новин)",
            callback_data=f"select_source:{i-1}"
        )])

    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])

    # Зберігаємо список джерел
    await state.update_data(sources_list=list(sources_dict.items())[:10])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data.startswith("select_source:"))
async def select_source_handler(callback: CallbackQuery, state: FSMContext):
    """Показати новини з вибраного джерела"""
    index = int(callback.data.split(":")[1])
    data = await state.get_data()
    sources_list = data.get('sources_list', [])
    news = data.get('search_results', [])

    if index >= len(sources_list):
        await callback.answer("❌ Джерело не знайдено")
        return

    source_name, articles = sources_list[index]

    text = f"📰 **{source_name}**\n\nЗнайдено {len(articles)} новин:\n\n"

    keyboard = []
    for i, article in enumerate(articles[:10], 1):
        title_uk = article.get('title_uk', article.get('title', 'No title'))[:50]
        # Знаходимо індекс статті в загальному списку
        article_index = news.index(article) if article in news else i-1
        keyboard.append([InlineKeyboardButton(
            text=f"{i}. {title_uk}...",
            callback_data=f"select_search_result:{article_index}"
        )])

    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="show_by_sources")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data.startswith("select_search_result:"))
async def select_search_result_handler(callback: CallbackQuery, state: FSMContext):
    """Вибрати новину з результатів пошуку"""
    index = int(callback.data.split(":")[1])
    data = await state.get_data()
    news = data.get('search_results', [])

    if index >= len(news):
        await callback.answer("❌ Новину не знайдено")
        return

    article = news[index]

    await state.update_data(selected_article=article, news_list=news)

    # Використовуємо перекладені заголовки
    display_title = article.get('title_uk', article.get('title', ''))
    display_description = article.get('description_uk', article.get('description', ''))
    original_title = article.get('title', '')

    # Показуємо оригінал якщо він відрізняється
    original_text = ""
    if original_title != display_title:
        original_text = f"\n\n🌐 **Оригінал:**\n_{original_title[:100]}..._\n"

    text = f"""📰 **{display_title}**

📝 {display_description[:300]}...{original_text}

🕐 {article.get('publishedAt', '')}
📰 {article.get('source', {}).get('name', 'Unknown')}

Що робити далі?"""

    article_url = article.get('url', '')

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Перейти до першоджерела", url=article_url)] if article_url else [],
        [InlineKeyboardButton(text="🔍 Знайти джерела", callback_data=f"find_sources_search:{index}")],
        [InlineKeyboardButton(text="✍️ Написати статтю", callback_data=f"write_article_search:{index}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="show_search_results")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data.startswith("find_sources_search:"))
async def find_sources_search_handler(callback: CallbackQuery, state: FSMContext):
    """Знайти додаткові джерела для новини з пошуку"""
    data = await state.get_data()
    article = data.get('selected_article', {})
    title = article.get('title_uk', article.get('title', ''))

    # Витягуємо ключові слова
    query_words = extract_keywords_from_title(title, max_words=5)

    # Зберігаємо запит та контекст для можливості редагування
    await state.update_data(current_search_query=query_words, search_context='search')

    await callback.message.edit_text(f"🔍 Шукаю додаткові джерела за запитом: *{query_words}*...", parse_mode="Markdown")

    gnews = GNewsService(Config.GNEWS_API_KEY)
    sources = await gnews.search_news(query=query_words, max_results=15)

    if not sources:
        await callback.message.edit_text(
            f"❌ Додаткові джерела не знайдено за запитом: *{query_words}*\n\n"
            f"Спробуйте змінити запит вручну або оберіть іншу дію.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Змінити запит", callback_data="edit_source_query")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="show_search_results")]
            ]),
            parse_mode="Markdown"
        )
        return

    await state.update_data(sources=sources)

    text = f"🔍 **Знайдено {len(sources)} джерел:**\n\n"
    text += f"_Запит: {query_words}_\n\n"

    for i, src in enumerate(sources[:10], 1):
        text += f"{i}️⃣ **{src.get('source', {}).get('name', 'Unknown')}**\n"
        text += f"_{src.get('title', '')[:60]}..._\n"
        if src.get('image'):
            text += "🖼️ Є зображення\n"
        text += "\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Змінити запит", callback_data="edit_source_query")],
        [InlineKeyboardButton(text="🖼️ Показати зображення", callback_data="show_images")],
        [InlineKeyboardButton(text="✍️ Написати статтю", callback_data="write_from_sources")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="show_search_results")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data.startswith("write_article_search:"))
async def write_article_search_handler(callback: CallbackQuery, state: FSMContext, db: Database):
    """Написати статтю одразу з результатів пошуку"""
    await callback.message.edit_text("🔍 Шукаю джерела та генерую статтю...")

    data = await state.get_data()
    article = data.get('selected_article', {})

    gnews = GNewsService(Config.GNEWS_API_KEY)
    title = article.get('title_uk', article.get('title', ''))
    query_words = extract_keywords_from_title(title, max_words=5)

    sources = await gnews.search_news(query=query_words, max_results=15)

    if not sources:
        await callback.message.edit_text(
            "❌ Джерела не знайдено. Спробуйте іншу новину.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="show_search_results")]
            ])
        )
        return

    await state.update_data(sources=sources)
    # Викликаємо функцію генерації статті
    await write_article_handler(callback, state, db)

# ========== Редагування запиту пошуку джерел ==========

@router.callback_query(F.data == "edit_source_query")
async def edit_source_query_handler(callback: CallbackQuery, state: FSMContext):
    """Запитати користувача про новий запит для пошуку джерел"""
    data = await state.get_data()
    current_query = data.get('current_search_query', '')

    await state.set_state(ArticleStates.editing_source_query)

    await callback.message.edit_text(
        f"✏️ **Редагування запиту пошуку**\n\n"
        f"Поточний запит: _{current_query}_\n\n"
        f"Введіть новий запит для пошуку джерел:",
        parse_mode="Markdown"
    )

@router.message(ArticleStates.editing_source_query)
async def process_manual_source_query(message: Message, state: FSMContext):
    """Обробити вручну введений запит та виконати пошук джерел"""
    new_query = message.text.strip()

    if not new_query:
        await message.answer("❌ Запит не може бути порожнім. Спробуйте ще раз:")
        return

    data = await state.get_data()
    search_context = data.get('search_context', 'geo')

    # Оновлюємо запит
    await state.update_data(current_search_query=new_query)

    await message.answer(f"🔍 Шукаю джерела за новим запитом: *{new_query}*...", parse_mode="Markdown")

    gnews = GNewsService(Config.GNEWS_API_KEY)
    sources = await gnews.search_news(query=new_query, max_results=15)

    # Повертаємося до нормального стану
    await state.set_state(None)

    if not sources:
        back_button_text = "🔙 Назад до списку" if search_context == 'geo' else "🔙 Назад"
        back_button_callback = "back_to_news_list" if search_context == 'geo' else "show_search_results"

        await message.answer(
            f"❌ Джерела не знайдено за запитом: *{new_query}*\n\n"
            f"Спробуйте змінити запит ще раз або оберіть іншу дію.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Змінити запит", callback_data="edit_source_query")],
                [InlineKeyboardButton(text=back_button_text, callback_data=back_button_callback)]
            ]),
            parse_mode="Markdown"
        )
        return

    await state.update_data(sources=sources)

    text = f"🔍 **Знайдено {len(sources)} джерел:**\n\n"
    text += f"_Запит: {new_query}_\n\n"

    for i, src in enumerate(sources[:10], 1):
        text += f"{i}️⃣ **{src.get('source', {}).get('name', 'Unknown')}**\n"
        text += f"_{src.get('title', '')[:60]}..._\n"
        if src.get('image'):
            text += "🖼️ Є зображення\n"
        text += "\n"

    back_button_text = "🔙 Назад до списку" if search_context == 'geo' else "🔙 Назад"
    back_button_callback = "back_to_news_list" if search_context == 'geo' else "show_search_results"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Змінити запит", callback_data="edit_source_query")],
        [InlineKeyboardButton(text="🖼️ Показати зображення", callback_data="show_images")],
        [InlineKeyboardButton(text="✍️ Написати статтю", callback_data="write_from_sources")],
        [InlineKeyboardButton(text=back_button_text, callback_data=back_button_callback)]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

# ========== Генерація статті ==========

@router.callback_query(F.data == "write_from_sources")
async def write_article_handler(callback: CallbackQuery, state: FSMContext, db: Database):
    """Написати статтю з джерел"""
    await callback.message.edit_text("✍️ Генерую статтю через GPT-4...")

    data = await state.get_data()
    sources = data.get('sources', [])

    if not sources:
        await callback.answer("❌ Джерела не знайдено", show_alert=True)
        return

    try:
        gpt = GPTService(Config.OPENAI_API_KEY)
        article = await gpt.generate_article(sources)

        # Визначити ID категорії
        category_id = await db.get_category_id_by_name(article['category'])
        article['category_id'] = category_id

        await state.update_data(generated_article=article)

        # Превью
        content_preview = strip_html_tags(article['content'])[:300]

        text = f"""✅ **СТАТТЮ СТВОРЕНО!**

📰 **Заголовок:**
{article['title']}

📁 **Категорія:** {article['category']}

📝 **Опис:**
{article['excerpt']}

📄 **Превью:**
{content_preview}...

🔍 **SEO:** {article['seo_description']}
"""

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📁 Змінити категорію", callback_data="change_category")],
            [
                InlineKeyboardButton(text="✏️ Редагувати", callback_data="edit_article"),
                InlineKeyboardButton(text="💾 Чернетка", callback_data="save_draft")
            ],
            [InlineKeyboardButton(text="📱 Опублікувати", callback_data="publish_menu")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="fetch_news")]
        ])

        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Article generation failed: {e}")
        await callback.message.edit_text(
            f"❌ Помилка генерації: {str(e)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Спробувати ще", callback_data="write_from_sources")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="fetch_news")]
            ])
        )

# ========== Зміна категорії ==========

@router.callback_query(F.data == "change_category")
async def change_category_handler(callback: CallbackQuery, db: Database):
    """Змінити категорію"""
    categories = await db.get_all_categories()

    keyboard = []
    for cat in categories:
        keyboard.append([InlineKeyboardButton(
            text=f"📁 {cat['name']}",
            callback_data=f"select_cat:{cat['id']}"
        )])

    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="write_from_sources")])

    await callback.message.edit_text(
        "📁 **Оберіть категорію:**",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("select_cat:"))
async def select_category_handler(callback: CallbackQuery, state: FSMContext, db: Database):
    """Вибрати категорію"""
    category_id = int(callback.data.split(":")[1])
    category = await db.get_category_by_id(category_id)

    data = await state.get_data()
    article = data.get('generated_article', {})
    article['category'] = category['name']
    article['category_id'] = category_id

    await state.update_data(generated_article=article)
    await callback.answer(f"✅ Категорія: {category['name']}")

    await write_article_handler(callback, state, db)

@router.callback_query(F.data == "edit_article")
async def edit_article_handler(callback: CallbackQuery, state: FSMContext):
    """Меню редагування статті"""
    text = "✏️ **РЕДАГУВАННЯ СТАТТІ**\n\nЩо ви хочете змінити?"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Заголовок", callback_data="edit_title")],
        [InlineKeyboardButton(text="📄 Зміст", callback_data="edit_content")],
        [InlineKeyboardButton(text="📋 Опис", callback_data="edit_excerpt")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="write_from_sources")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "edit_title")
async def edit_title_prompt(callback: CallbackQuery, state: FSMContext):
    """Початок редагування заголовка"""
    await callback.message.edit_text(
        "✏️ Надішліть новий заголовок для статті:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="edit_article")]
        ])
    )
    await state.set_state(ArticleStates.editing_title)

@router.message(ArticleStates.editing_title)
async def process_title_edit(message: Message, state: FSMContext):
    """Обробка нового заголовка"""
    data = await state.get_data()
    article = data.get('generated_article', {})
    article['title'] = message.text
    await state.update_data(generated_article=article)
    await state.clear()

    await message.answer(
        f"✅ Заголовок оновлено!\n\n**Новий заголовок:**\n{message.text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редагувати ще", callback_data="edit_article")],
            [InlineKeyboardButton(text="📱 Опублікувати", callback_data="publish_menu")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="write_from_sources")]
        ]),
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "edit_excerpt")
async def edit_excerpt_prompt(callback: CallbackQuery, state: FSMContext):
    """Початок редагування опису"""
    await callback.message.edit_text(
        "✏️ Надішліть новий короткий опис для статті:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="edit_article")]
        ])
    )
    await state.set_state(ArticleStates.editing_excerpt)

@router.message(ArticleStates.editing_excerpt)
async def process_excerpt_edit(message: Message, state: FSMContext):
    """Обробка нового опису"""
    data = await state.get_data()
    article = data.get('generated_article', {})
    article['excerpt'] = message.text
    await state.update_data(generated_article=article)
    await state.clear()

    await message.answer(
        f"✅ Опис оновлено!\n\n**Новий опис:**\n{message.text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редагувати ще", callback_data="edit_article")],
            [InlineKeyboardButton(text="📱 Опублікувати", callback_data="publish_menu")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="write_from_sources")]
        ]),
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "edit_content")
async def edit_content_info(callback: CallbackQuery):
    """Інформація про редагування контенту"""
    await callback.answer(
        "⚠️ Редагування повного контенту можливе тільки після публікації на WordPress",
        show_alert=True
    )

# ========== Публікація ==========

@router.callback_query(F.data == "publish_menu")
async def publish_menu_handler(callback: CallbackQuery):
    """Меню публікації"""
    text = "📱 **КУДИ ОПУБЛІКУВАТИ?**\n\nОберіть один або обидва варіанти:"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 WordPress (spilno.online)", callback_data="publish_wp")],
        [InlineKeyboardButton(text="📱 Telegram канал", callback_data="publish_tg")],
        [InlineKeyboardButton(text="🚀 Обидва", callback_data="publish_both")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="write_from_sources")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data.startswith("publish_"))
async def publish_handler(callback: CallbackQuery, state: FSMContext, db: Database):
    """Публікація статті"""
    publish_type = callback.data.split("_")[1]  # wp, tg, both

    data = await state.get_data()
    article = data.get('generated_article')
    selected_image = data.get('selected_image')
    sources = data.get('sources', [])

    if not article:
        await callback.answer("❌ Статтю не знайдено", show_alert=True)
        return

    progress_msg = await callback.message.edit_text("📤 Публікую...")

    try:
        wp_service = WordPressService(Config.WP_SITE_URL, Config.WP_USERNAME, Config.WP_APP_PASSWORD)

        wp_post_id = None
        wp_url = None
        tg_message_id = None
        featured_media_id = None

        # WordPress
        if publish_type in ['wp', 'both']:
            await progress_msg.edit_text("📤 Завантажую зображення...")

            if selected_image:
                try:
                    featured_media_id = await wp_service.upload_image(selected_image, callback.bot)
                except Exception as e:
                    logger.warning(f"Image upload failed: {e}")

            await progress_msg.edit_text("📤 Створюю пост на WordPress...")
            wp_post = await wp_service.create_post(article, featured_media_id)
            wp_post_id = wp_post['id']
            wp_url = wp_post['link']

        # Telegram
        if publish_type in ['tg', 'both']:
            await progress_msg.edit_text("📤 Публікую в Telegram...")
            # Конвертуємо HTML для Telegram
            telegram_content = html_to_telegram(article['content'], max_length=3800)

            # Додаємо брендинг та посилання
            if wp_url:
                telegram_content += f"\n\n📰 <b>Читати повністю:</b> {wp_url}\n\n"
            telegram_content += f"<b>Джерело:</b> <a href='{Config.WP_SITE_URL}'>Спільно</a>"

            tg_msg = await callback.bot.send_message(
                Config.CHANNEL_ID,
                telegram_content,
                parse_mode="HTML",
                disable_web_page_preview=False
            )
            tg_message_id = tg_msg.message_id

        # Збереження
        await progress_msg.edit_text("📤 Зберігаю в БД...")
        await db.save_published_article(
            user_id=callback.from_user.id,
            wp_post_id=wp_post_id,
            tg_message_id=tg_message_id,
            title=article['title'],
            url=wp_url,
            category_id=article['category_id'],
            published_to_wp=(publish_type in ['wp', 'both']),
            published_to_tg=(publish_type in ['tg', 'both']),
            sources=sources
        )

        await db.add_article_fingerprint(article['title'])
        await db.log_action(callback.from_user.id, 'publish', 'success',
                           f"Published: {article['title']}", {'wp_post_id': wp_post_id})

        # Результат
        result_text = f"✅ **ОПУБЛІКОВАНО!**\n\n📰 **{article['title']}**\n\n"
        if wp_url:
            result_text += f"🌐 WordPress: {wp_url}\n"
        if tg_message_id:
            result_text += f"📱 Telegram: опубліковано\n"
        result_text += f"\n📁 Категорія: {article['category']}\n"
        result_text += f"📊 Джерел: {len(sources)}"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")]
        ])

        await progress_msg.edit_text(result_text, reply_markup=keyboard, parse_mode="Markdown")

        await state.clear()

    except Exception as e:
        logger.error(f"Publishing failed: {e}")
        await db.log_action(callback.from_user.id, 'publish', 'error', str(e), {})

        await progress_msg.edit_text(
            f"❌ Помилка публікації:\n\n{str(e)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Спробувати ще", callback_data="publish_menu")],
                [InlineKeyboardButton(text="💾 Зберегти чернетку", callback_data="save_draft")]
            ])
        )

# ========== Чернетки ==========

@router.callback_query(F.data == "save_draft")
async def save_draft_handler(callback: CallbackQuery, state: FSMContext, db: Database):
    """Зберегти чернетку"""
    data = await state.get_data()
    article = data.get('generated_article')
    selected_image = data.get('selected_image')
    sources = data.get('sources', [])

    if not article:
        await callback.answer("❌ Статтю не знайдено", show_alert=True)
        return

    try:
        draft_id = await db.save_draft(
            user_id=callback.from_user.id,
            title=article['title'],
            content=article['content'],
            excerpt=article['excerpt'],
            category_id=article['category_id'],
            seo_description=article.get('seo_description', ''),
            images=[selected_image] if selected_image else [],
            sources=sources
        )

        await callback.message.edit_text(
            f"💾 **Чернетку збережено!**\n\nID: {draft_id}\n\n"
            f"Знайти в: 📝 Чернетки",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Головне меню", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )

    except Exception as e:
        await callback.answer(f"❌ Помилка: {str(e)}", show_alert=True)

@router.callback_query(F.data == "show_drafts")
async def show_drafts_handler(callback: CallbackQuery, db: Database):
    """Показати чернетки"""
    drafts = await db.get_user_drafts(callback.from_user.id)

    if not drafts:
        await callback.message.edit_text(
            "📝 **ЧЕРНЕТКИ**\n\nУ вас немає збережених чернеток.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return

    text = f"📝 **ЧЕРНЕТКИ** ({len(drafts)})\n\n"

    keyboard = []
    for draft in drafts[:10]:
        text += f"🆔 {draft['id']}: {draft['title'][:40]}...\n"
        text += f"📁 {draft.get('category_name', 'N/A')} | {draft['created_at'].strftime('%d.%m %H:%M')}\n\n"

        keyboard.append([InlineKeyboardButton(
            text=f"📄 {draft['id']}: {draft['title'][:30]}",
            callback_data=f"open_draft:{draft['id']}"
        )])

    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data == "show_published")
async def show_published_handler(callback: CallbackQuery, db: Database):
    """Показати опубліковані статті"""
    articles = await db.get_user_published_articles(callback.from_user.id, limit=10)

    if not articles:
        await callback.message.edit_text(
            "🌐 **ОПУБЛІКОВАНІ СТАТТІ**\n\nУ вас немає опублікованих статей.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return

    text = f"🌐 **ОПУБЛІКОВАНІ СТАТТІ** ({len(articles)})\n\n"

    keyboard = []
    for article in articles:
        pub_date = article['published_at'].strftime('%d.%m %H:%M')
        status_wp = "🌐" if article['published_to_wp'] else ""
        status_tg = "📱" if article['published_to_tg'] else ""

        text += f"{status_wp}{status_tg} {article['title'][:40]}...\n"
        text += f"📁 {article.get('category_name', 'N/A')} | {pub_date}\n"
        if article.get('url'):
            text += f"🔗 {article['url']}\n"
        text += "\n"

        keyboard.append([InlineKeyboardButton(
            text=f"{status_wp}{status_tg} {article['title'][:35]}",
            callback_data=f"view_article:{article['id']}"
        )])

    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@router.callback_query(F.data.startswith("view_article:"))
async def view_article_handler(callback: CallbackQuery, db: Database):
    """Переглянути опубліковану статтю"""
    article_id = int(callback.data.split(":")[1])
    article = await db.get_published_article(article_id)

    if not article:
        await callback.answer("❌ Статтю не знайдено", show_alert=True)
        return

    pub_date = article['published_at'].strftime('%d.%m.%Y %H:%M')
    status_wp = "✅" if article['published_to_wp'] else "⬜"
    status_tg = "✅" if article['published_to_tg'] else "⬜"

    text = f"""📰 **ОПУБЛІКОВАНА СТАТТЯ**

**Заголовок:**
{article['title']}

**Категорія:** {article.get('category_name', 'N/A')}
**Дата:** {pub_date}

**Опубліковано:**
{status_wp} WordPress
{status_tg} Telegram
"""

    if article.get('url'):
        text += f"\n**Посилання:**\n{article['url']}"

    if article.get('views') or article.get('clicks'):
        text += f"\n\n**Статистика:**\n👁️ Переглядів: {article.get('views', 0)}\n🔗 Кліків: {article.get('clicks', 0)}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Видалити", callback_data=f"delete_article:{article_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="show_published")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data.startswith("delete_article:"))
async def delete_article_handler(callback: CallbackQuery, db: Database):
    """Видалити опубліковану статтю"""
    article_id = int(callback.data.split(":")[1])
    article = await db.get_published_article(article_id)

    if not article:
        await callback.answer("❌ Статтю не знайдено", show_alert=True)
        return

    # Формуємо інформацію про те, звідки буде видалено статтю
    delete_locations = []
    if article['published_to_wp'] and article.get('wp_post_id'):
        delete_locations.append("🌐 WordPress")
    if article['published_to_tg'] and article.get('tg_message_id'):
        delete_locations.append("📱 Telegram")
    delete_locations.append("💾 База даних бота")

    locations_text = "\n".join(f"• {loc}" for loc in delete_locations)

    # Підтвердження
    text = f"""⚠️ **ВИДАЛЕННЯ СТАТТІ**

❗ Ви впевнені, що хочете видалити статтю?

📰 **{article['title'][:50]}...**

**Буде видалено з:**
{locations_text}

⚠️ **Ця дія незворотна!**"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так, видалити повністю", callback_data=f"confirm_delete_article:{article_id}")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data=f"view_article:{article_id}")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data.startswith("confirm_delete_article:"))
async def confirm_delete_article_handler(callback: CallbackQuery, db: Database):
    """Підтвердження видалення статті"""
    article_id = int(callback.data.split(":")[1])
    article = await db.get_published_article(article_id)

    if not article:
        await callback.answer("❌ Статтю не знайдено", show_alert=True)
        return

    progress_msg = await callback.message.edit_text("🗑️ Видаляю статтю...")

    results = []
    errors = []

    try:
        # Видалення з WordPress
        if article['published_to_wp'] and article.get('wp_post_id'):
            await progress_msg.edit_text("🗑️ Видаляю з WordPress...")
            try:
                wp_service = WordPressService(Config.WP_SITE_URL, Config.WP_USERNAME, Config.WP_APP_PASSWORD)
                success = await wp_service.delete_post(article['wp_post_id'])
                if success:
                    results.append("✅ WordPress")
                else:
                    errors.append("⚠️ WordPress (помилка видалення)")
            except Exception as e:
                logger.error(f"WP delete error: {e}")
                errors.append(f"❌ WordPress ({str(e)[:30]})")

        # Видалення з Telegram
        if article['published_to_tg'] and article.get('tg_message_id'):
            await progress_msg.edit_text("🗑️ Видаляю з Telegram...")
            try:
                await callback.bot.delete_message(Config.CHANNEL_ID, article['tg_message_id'])
                results.append("✅ Telegram")
            except Exception as e:
                logger.error(f"TG delete error: {e}")
                errors.append(f"❌ Telegram ({str(e)[:30]})")

        # Видалення з БД
        await progress_msg.edit_text("🗑️ Видаляю з бази даних...")
        await db.delete_published_article(article_id)
        results.append("✅ База даних")

        # Формуємо повідомлення про результат
        # Escape спеціальних символів для Markdown
        result_text = "🗑️ **ВИДАЛЕННЯ ЗАВЕРШЕНО**\n\n"

        if results:
            # Escape кожного результату окремо
            escaped_results = [html.escape(r) for r in results]
            result_text += "**Успішно видалено:**\n" + "\n".join(escaped_results) + "\n\n"

        if errors:
            # Escape кожної помилки окремо
            escaped_errors = [html.escape(e) for e in errors]
            result_text += "**Помилки:**\n" + "\n".join(escaped_errors)

        # Логування
        await db.log_action(
            callback.from_user.id,
            'delete_article',
            'success' if not errors else 'error',
            f"Deleted: {article['title']}",
            {'results': results, 'errors': errors}
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 До опублікованих", callback_data="show_published")]
        ])

        await progress_msg.edit_text(result_text, reply_markup=keyboard, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Delete article error: {e}")
        await db.log_action(callback.from_user.id, 'delete_article', 'error', str(e), {})

        # Escape помилки для Markdown
        escaped_error = html.escape(str(e))
        await progress_msg.edit_text(
            f"❌ **Критична помилка видалення:**\n\n{escaped_error}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="show_published")]
            ]),
            parse_mode="Markdown"
        )

@router.callback_query(F.data.startswith("open_draft:"))
async def open_draft_handler(callback: CallbackQuery, db: Database):
    """Відкрити чернетку"""
    draft_id = int(callback.data.split(":")[1])
    draft = await db.get_draft_by_id(draft_id)

    if not draft:
        await callback.answer("❌ Чернетку не знайдено", show_alert=True)
        return

    created_date = draft['created_at'].strftime('%d.%m.%Y %H:%M')
    content_preview = strip_html_tags(draft['content'])[:200]

    text = f"""📝 **ЧЕРНЕТКА #{draft_id}**

**Заголовок:**
{draft['title']}

**Категорія:** {draft.get('category_name', 'N/A')}
**Створено:** {created_date}

**Опис:**
{draft['excerpt']}

**Превью контенту:**
{content_preview}...
"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Опублікувати", callback_data=f"publish_draft:{draft_id}")],
        [InlineKeyboardButton(text="🗑️ Видалити чернетку", callback_data=f"delete_draft:{draft_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="show_drafts")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data.startswith("delete_draft:"))
async def delete_draft_handler(callback: CallbackQuery, db: Database):
    """Видалити чернетку"""
    draft_id = int(callback.data.split(":")[1])

    try:
        await db.delete_draft(draft_id)
        await callback.answer("✅ Чернетку видалено")
        await show_drafts_handler(callback, db)
    except Exception as e:
        await callback.answer(f"❌ Помилка: {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("publish_draft:"))
async def publish_draft_handler(callback: CallbackQuery, state: FSMContext, db: Database):
    """Опублікувати чернетку"""
    draft_id = int(callback.data.split(":")[1])
    draft = await db.get_draft_by_id(draft_id)

    if not draft:
        await callback.answer("❌ Чернетку не знайдено", show_alert=True)
        return

    # Завантажуємо чернетку в стан як згенеровану статтю
    article = {
        'title': draft['title'],
        'content': draft['content'],
        'excerpt': draft['excerpt'],
        'category_id': draft['category_id'],
        'category': draft.get('category_name', ''),
        'seo_description': draft.get('seo_description', '')
    }

    await state.update_data(
        generated_article=article,
        selected_image=draft['images'][0] if draft['images'] else None,
        sources=draft.get('sources', [])
    )

    # Показуємо меню публікації
    text = "📱 **КУДИ ОПУБЛІКУВАТИ ЧЕРНЕТКУ?**\n\nОберіть один або обидва варіанти:"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 WordPress (spilno.online)", callback_data="publish_wp")],
        [InlineKeyboardButton(text="📱 Telegram канал", callback_data="publish_tg")],
        [InlineKeyboardButton(text="🚀 Обидва", callback_data="publish_both")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"open_draft:{draft_id}")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

# ========== Налаштування ==========

@router.callback_query(F.data == "settings")
async def settings_handler(callback: CallbackQuery, db: Database):
    """Налаштування"""
    settings = await db.get_user_settings(callback.from_user.id)

    auto_status = "🟢 Увімкнено" if settings['auto_publish_enabled'] else "⚪ Вимкнено"
    interval = settings['auto_publish_interval']
    hours = interval // 60

    wp_status = "✅" if settings['auto_publish_to_wp'] else "⬜"
    tg_status = "✅" if settings['auto_publish_to_tg'] else "⬜"

    text = f"""⚙️ **НАЛАШТУВАННЯ**

🤖 **Автопублікація:** {auto_status}
⏰ **Інтервал:** {interval} хв ({hours} год)

**Цілі публікації:**
{wp_status} WordPress
{tg_status} Telegram

**Категорії:** {len(settings.get('enabled_categories', []))} активних"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🤖 Автопубл: {'Вимк' if settings['auto_publish_enabled'] else 'Увімк'}",
            callback_data="toggle_auto"
        )],
        [InlineKeyboardButton(text="⏰ Інтервал", callback_data="change_interval")],
        [InlineKeyboardButton(text="🎯 Цілі публікації", callback_data="publish_targets")],
        [InlineKeyboardButton(text="📁 Категорії", callback_data="configure_categories")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "toggle_auto")
async def toggle_auto_handler(callback: CallbackQuery, db: Database, scheduler: AutoPublishScheduler):
    """Перемикнути автопублікацію"""
    user_id = callback.from_user.id
    settings = await db.get_user_settings(user_id)

    new_status = not settings['auto_publish_enabled']
    await db.update_user_setting(user_id, 'auto_publish_enabled', new_status)

    if new_status:
        scheduler.start_user_schedule(user_id, settings['auto_publish_interval'])
        await callback.answer("✅ Автопублікацію увімкнено")
    else:
        scheduler.stop_user_schedule(user_id)
        await callback.answer("⚪ Автопублікацію вимкнено")

    await settings_handler(callback, db)

@router.callback_query(F.data == "change_interval")
async def change_interval_handler(callback: CallbackQuery):
    """Змінити інтервал"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏰ 1 година", callback_data="set_interval:60")],
        [InlineKeyboardButton(text="⏰ 2 години", callback_data="set_interval:120")],
        [InlineKeyboardButton(text="⏰ 3 години", callback_data="set_interval:180")],
        [InlineKeyboardButton(text="⏰ 6 годин", callback_data="set_interval:360")],
        [InlineKeyboardButton(text="⏰ 12 годин", callback_data="set_interval:720")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="settings")]
    ])

    await callback.message.edit_text(
        "⏰ **Оберіть інтервал автопублікації:**",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("set_interval:"))
async def set_interval_handler(callback: CallbackQuery, db: Database, scheduler: AutoPublishScheduler):
    """Встановити інтервал"""
    interval = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    await db.update_user_setting(user_id, 'auto_publish_interval', interval)

    settings = await db.get_user_settings(user_id)
    if settings['auto_publish_enabled']:
        scheduler.start_user_schedule(user_id, interval)

    hours = interval // 60
    await callback.answer(f"✅ Інтервал: {hours} год")
    await settings_handler(callback, db)

@router.callback_query(F.data == "publish_targets")
async def publish_targets_handler(callback: CallbackQuery, db: Database):
    """Цілі публікації"""
    settings = await db.get_user_settings(callback.from_user.id)

    wp_status = "✅" if settings['auto_publish_to_wp'] else "⬜"
    tg_status = "✅" if settings['auto_publish_to_tg'] else "⬜"

    text = f"""🎯 **ЦІЛІ ПУБЛІКАЦІЇ**

{wp_status} WordPress (spilno.online)
{tg_status} Telegram канал

Оберіть що змінити:"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{wp_status} WordPress", callback_data="toggle_wp")],
        [InlineKeyboardButton(text=f"{tg_status} Telegram", callback_data="toggle_tg")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="settings")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

@router.callback_query(F.data == "toggle_wp")
async def toggle_wp_handler(callback: CallbackQuery, db: Database):
    """Перемикнути WordPress"""
    user_id = callback.from_user.id
    settings = await db.get_user_settings(user_id)
    new_val = not settings['auto_publish_to_wp']
    await db.update_user_setting(user_id, 'auto_publish_to_wp', new_val)
    await callback.answer("✅ Змінено")
    await publish_targets_handler(callback, db)

@router.callback_query(F.data == "toggle_tg")
async def toggle_tg_handler(callback: CallbackQuery, db: Database):
    """Перемикнути Telegram"""
    user_id = callback.from_user.id
    settings = await db.get_user_settings(user_id)
    new_val = not settings['auto_publish_to_tg']
    await db.update_user_setting(user_id, 'auto_publish_to_tg', new_val)
    await callback.answer("✅ Змінено")
    await publish_targets_handler(callback, db)

# ========== Статистика ==========

@router.callback_query(F.data == "statistics")
async def statistics_handler(callback: CallbackQuery):
    """Меню статистики"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Сьогодні", callback_data="stats:today")],
        [InlineKeyboardButton(text="📊 Тиждень", callback_data="stats:week")],
        [InlineKeyboardButton(text="📊 Місяць", callback_data="stats:month")],
        [InlineKeyboardButton(text="📊 Весь час", callback_data="stats:all")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

    await callback.message.edit_text(
        "📊 **СТАТИСТИКА**\n\nОберіть період:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("stats:"))
async def show_stats_handler(callback: CallbackQuery, db: Database):
    """Показати статистику"""
    period = callback.data.split(":")[1]
    stats = await db.get_statistics(callback.from_user.id, period)

    period_names = {
        'today': 'Сьогодні',
        'week': 'Тиждень',
        'month': 'Місяць',
        'all': 'Весь час'
    }

    text = f"""📊 **СТАТИСТИКА: {period_names[period]}**

📰 Статей: {stats['total_articles']}
👁️ Переглядів: {stats['total_views']:,}
🔗 Кліків: {stats['total_clicks']:,}

**По категоріях:**
"""

    for cat in stats['by_category']:
        text += f"📁 {cat['name']}: {cat['count']}\n"

    if stats['top_article']:
        top = stats['top_article']
        text += f"\n**Топ стаття:**\n📰 {top['title'][:50]}...\n👁️ {top.get('views', 0)} переглядів"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="statistics")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")

# ========== MAIN ==========

async def main():
    """Головна функція"""
    # Валідація конфігурації
    Config.validate()

    # База даних
    db = Database(Config.DATABASE_URL)
    await db.connect()

    # Бот
    bot = Bot(token=Config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Scheduler
    scheduler = AutoPublishScheduler(bot, db)
    scheduler.start()

    # Middleware для передачі db та scheduler
    @dp.update.outer_middleware()
    async def db_middleware(handler, event, data):
        data['db'] = db
        data['scheduler'] = scheduler
        return await handler(event, data)

    # Реєстрація роутера
    dp.include_router(router)

    logger.info("🚀 Бот запущено!")
    logger.info(f"📱 Telegram: @{(await bot.get_me()).username}")
    logger.info(f"🌐 WordPress: {Config.WP_SITE_URL}")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await db.close()
        await bot.session.close()
        logger.info("Бот зупинено")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Зупинка за Ctrl+C")
    except Exception as e:
        logger.error(f"Критична помилка: {e}")
        import traceback
        traceback.print_exc()


