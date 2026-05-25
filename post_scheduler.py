import asyncio
import logging
from datetime import datetime, timedelta
from sqlalchemy import select
from database import AsyncSessionLocal
from models import PostQueue, Project, PublishedPost
from posters import TelegramPoster

logger = logging.getLogger(__name__)


class PostScheduler:
    """Планировщик публикации постов из очереди с соблюдением интервала."""
    
    def __init__(self, telegram_poster: TelegramPoster):
        self.telegram_poster = telegram_poster
        self._running = False

    async def start(self):
        self._running = True
        logger.info("🟢 PostScheduler started")
        
        while self._running:
            try:
                await self._check_and_publish()
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"PostScheduler error: {e}")
                await asyncio.sleep(60)

    async def _check_and_publish(self):
        """Публикует ОДИН пост за раз с проверкой интервала."""
        async with AsyncSessionLocal() as session:
            # Берём самый старый pending пост
            result = await session.execute(
                select(PostQueue).where(
                    PostQueue.status == "pending",
                    PostQueue.scheduled_time <= datetime.utcnow()
                ).order_by(PostQueue.scheduled_time).limit(1)
            )
            queue_item = result.scalar_one_or_none()
        
        if not queue_item:
            return
        
        # Проверяем интервал от последнего опубликованного поста в проекте
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Project).where(Project.id == queue_item.project_id)
            )
            project = result.scalar_one_or_none()
            
            if project:
                result = await session.execute(
                    select(PublishedPost).where(
                        PublishedPost.project_id == project.id
                    ).order_by(PublishedPost.published_at.desc()).limit(1)
                )
                last_published = result.scalar_one_or_none()
                
                if last_published and last_published.published_at:
                    interval_minutes = max(
                        int(project.post_interval_hours * 60),
                        30
                    )
                    msk_now = datetime.utcnow() + timedelta(hours=3)
                    last_msk = last_published.published_at + timedelta(hours=3)
                    elapsed = (msk_now - last_msk).total_seconds() / 60
                    
                    if elapsed < interval_minutes:
                        logger.info(
                            f"⏳ Post {queue_item.id}: only {elapsed:.0f}min since last, "
                            f"need {interval_minutes}min for project '{project.name}'"
                        )
                        return
        
        # Публикуем
        try:
            logger.info(f"📤 Publishing post {queue_item.id} (scheduled: {queue_item.scheduled_time})")
            success = await self.telegram_poster.publish_post(queue_item)
            if success:
                logger.info(f"✅ Published post {queue_item.id}")
                
                # Обновляем счётчик в проекте
                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(Project).where(Project.id == queue_item.project_id)
                    )
                    db_project = result.scalar_one_or_none()
                    if db_project:
                        db_project.posts_posted_today += 1
                        await session.commit()
            else:
                logger.warning(f"❌ Failed to publish post {queue_item.id}")
        except Exception as e:
            logger.error(f"Error publishing post {queue_item.id}: {e}")

    async def stop(self):
        self._running = False
        logger.info("🔴 PostScheduler stopped")