import os
import logging
import shutil
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, text
from datetime import datetime, timedelta
from config import Config
from models import Base, User, Project, SourceChannel, TargetChannel, ParsedPost

logger = logging.getLogger(__name__)

os.makedirs(Config.DATA_DIR, exist_ok=True)
os.makedirs(Config.TEMP_DIR, exist_ok=True)
os.makedirs(Config.BACKUP_DIR, exist_ok=True)

old_db_path = "bot.db"
if os.path.exists(old_db_path) and not os.path.exists(Config.DB_PATH):
    shutil.move(old_db_path, Config.DB_PATH)
    logger.info(f"Moved database from {old_db_path} to {Config.DB_PATH}")

engine = create_async_engine(f"sqlite+aiosqlite:///{Config.DB_PATH}", echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

parsed_urls = {}


async def migrate_to_projects():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='projects'")
        )
        if not result.scalar():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("Created new tables")
            
            result = await session.execute(select(User))
            users = result.scalars().all()
            
            for user in users:
                result = await session.execute(select(SourceChannel).where(SourceChannel.user_id == user.telegram_id))
                old_sources = result.scalars().all()
                result = await session.execute(select(TargetChannel).where(TargetChannel.user_id == user.telegram_id))
                old_targets = result.scalars().all()
                
                if old_sources or old_targets:
                    project = Project(user_id=user.telegram_id, name="Основной", check_interval_minutes=60)
                    session.add(project)
                    await session.flush()
                    for source in old_sources:
                        source.project_id = project.id
                    for target in old_targets:
                        target.project_id = project.id
                    logger.info(f"Migrated user {user.telegram_id}")
            
            await session.commit()
            logger.info("Migration completed")
        
        # Существующие миграции
        migrations = [
            "ALTER TABLE users ADD COLUMN max_projects INTEGER DEFAULT 1",
            "ALTER TABLE users ADD COLUMN max_sources_per_project INTEGER DEFAULT 3",
            "ALTER TABLE users ADD COLUMN trial_ends_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN subscription_active BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN subscription_ends_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN tariff TEXT DEFAULT 'trial'",
            "ALTER TABLE users ADD COLUMN min_post_interval_minutes INTEGER DEFAULT 120",
            "ALTER TABLE users ADD COLUMN min_check_interval_minutes INTEGER DEFAULT 60",
            "ALTER TABLE users ADD COLUMN last_trial_warning_sent TIMESTAMP",
            "ALTER TABLE users ADD COLUMN last_subscription_warning_sent TIMESTAMP",
            "ALTER TABLE projects ADD COLUMN signature TEXT",
            "ALTER TABLE source_channels ADD COLUMN media_filter TEXT DEFAULT 'all'",
            "ALTER TABLE source_channels ADD COLUMN remove_original_text BOOLEAN DEFAULT FALSE",
            "ALTER TABLE source_channels ADD COLUMN max_video_duration INTEGER",
            "ALTER TABLE source_channels ADD COLUMN exclude_phrases TEXT",
            "ALTER TABLE source_channels ADD COLUMN include_keywords TEXT",
            "ALTER TABLE source_channels ADD COLUMN max_age_hours INTEGER DEFAULT 24",
            "ALTER TABLE target_channels ADD COLUMN platform TEXT DEFAULT 'telegram'",
            "ALTER TABLE target_channels ADD COLUMN vk_token TEXT",
            "ALTER TABLE target_channels ADD COLUMN vk_group_id BIGINT",
            "ALTER TABLE target_channels ADD COLUMN vk_group_name TEXT",
            "ALTER TABLE post_queue ADD COLUMN platform TEXT DEFAULT 'telegram'",
            "ALTER TABLE published_posts ADD COLUMN platform TEXT DEFAULT 'telegram'",
            "ALTER TABLE parsed_posts ADD COLUMN project_id INTEGER REFERENCES projects(id)",
        ]
        
        for sql in migrations:
            try:
                await session.execute(text(sql))
            except Exception as e:
                if "duplicate column name" not in str(e).lower():
                    logger.debug(f"Migration {sql[:50]}... skipped: {e}")
        
        # НОВЫЕ МИГРАЦИИ ДЛЯ YOUTUBE
        youtube_migrations = [
            "ALTER TABLE source_channels ADD COLUMN source_type TEXT DEFAULT 'telegram'",
            "ALTER TABLE source_channels ADD COLUMN youtube_query TEXT",
            "ALTER TABLE source_channels ADD COLUMN youtube_category TEXT",
            "ALTER TABLE source_channels ADD COLUMN youtube_region TEXT",
            "ALTER TABLE source_channels ADD COLUMN youtube_max_results INTEGER DEFAULT 10",
            "ALTER TABLE source_channels ADD COLUMN min_views INTEGER DEFAULT 0",
            "ALTER TABLE source_channels ADD COLUMN min_likes INTEGER DEFAULT 0",
            "ALTER TABLE source_channels ADD COLUMN min_comments INTEGER DEFAULT 0",
            "ALTER TABLE source_channels ADD COLUMN video_quality TEXT DEFAULT '720p'",
        ]
        
        for sql in youtube_migrations:
            try:
                await session.execute(text(sql))
                logger.info(f"Added column: {sql.split('ADD COLUMN')[1].strip().split()[0]}")
            except Exception as e:
                if "duplicate column name" not in str(e).lower():
                    logger.warning(f"Failed to add column: {e}")
        
        # Обновляем существующие записи: source_type = 'telegram' где NULL
        try:
            await session.execute(
                text("UPDATE source_channels SET source_type = 'telegram' WHERE source_type IS NULL")
            )
            logger.info("Updated existing sources: set source_type = 'telegram'")
        except Exception as e:
            logger.warning(f"Failed to update source_type: {e}")
        
        # Обновляем существующие записи: youtube_max_results = 10 где NULL
        try:
            await session.execute(
                text("UPDATE source_channels SET youtube_max_results = 10 WHERE youtube_max_results IS NULL")
            )
        except Exception as e:
            logger.warning(f"Failed to update youtube_max_results: {e}")
        
        # Обновляем существующие записи: video_quality = '720p' где NULL
        try:
            await session.execute(
                text("UPDATE source_channels SET video_quality = '720p' WHERE video_quality IS NULL")
            )
        except Exception as e:
            logger.warning(f"Failed to update video_quality: {e}")
        
        try:
            await session.execute(text("UPDATE parsed_posts SET project_id = (SELECT project_id FROM source_channels WHERE source_channels.id = parsed_posts.source_channel_id) WHERE project_id IS NULL"))
        except:
            pass
        
        try:
            await session.execute(text("UPDATE users SET trial_ends_at = datetime(created_at, '+5 days') WHERE trial_ends_at IS NULL"))
        except:
            pass
        
        await session.commit()
        logger.info("YouTube migrations completed")


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate_to_projects()
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == Config.ADMIN_ID))
        admin = result.scalar_one_or_none()
        if not admin:
            admin = User(
                telegram_id=Config.ADMIN_ID, is_admin=True, tariff="unlimited",
                max_projects=999, max_sources_per_project=999,
                min_post_interval_minutes=1, min_check_interval_minutes=5,
                subscription_active=True,
                trial_ends_at=datetime.utcnow() + timedelta(days=36500)
            )
            session.add(admin)
            await session.commit()
            logger.info("Admin created")
        
        result = await session.execute(select(Project).where(Project.user_id == Config.ADMIN_ID).order_by(Project.id))
        if not result.scalars().all():
            project = Project(user_id=Config.ADMIN_ID, name="Админский")
            session.add(project)
            await session.commit()
            logger.info("Admin project created")


async def is_post_parsed(project_id: int, post_url: str) -> bool:
    cache_key = f"{project_id}:{post_url}"
    if cache_key in parsed_urls:
        return True
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ParsedPost).where(ParsedPost.project_id == project_id, ParsedPost.post_url == post_url))
        exists = result.scalar_one_or_none() is not None
        if exists:
            parsed_urls[cache_key] = True
        return exists


async def mark_post_parsed(project_id: int, source_channel_id: int, post_url: str):
    cache_key = f"{project_id}:{post_url}"
    parsed_urls[cache_key] = True
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ParsedPost).where(ParsedPost.project_id == project_id, ParsedPost.post_url == post_url))
        if result.scalar_one_or_none():
            return
        post = ParsedPost(project_id=project_id, source_channel_id=source_channel_id, post_url=post_url)
        session.add(post)
        try:
            await session.commit()
        except:
            await session.rollback()


async def clear_parsed_cache():
    parsed_urls.clear()


async def get_active_projects():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Project).where(Project.is_active == True))
        return result.scalars().all()


async def get_user_projects(telegram_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Project).where(Project.user_id == telegram_id, Project.is_active == True))
        return result.scalars().all()


async def get_project_sources(project_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SourceChannel).where(SourceChannel.project_id == project_id, SourceChannel.is_active == True))
        return result.scalars().all()


async def get_project_target(project_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TargetChannel).where(TargetChannel.project_id == project_id))
        return result.scalar_one_or_none()