import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta
from sqlalchemy import select, update
from database import AsyncSessionLocal, is_post_parsed, mark_post_parsed
from models import User, Project, SourceChannel, TargetChannel, PostQueue, PublishedPost
from scrapers import TelegramScraper
from scrapers.youtube_scraper import YouTubeScraper
from posters import TelegramPoster
from utils import calculate_score, get_moscow_time
from config import Config

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, poster: TelegramPoster):
        self.poster = poster
        self._running = False
        self._tasks = {}
        self._last_daily_report = None
        self._last_check = {}
        self.youtube_scraper = YouTubeScraper()

    async def start(self):
        self._running = True
        logger.info("🟢 Scheduler started")
        
        while self._running:
            try:
                await self._check_projects()
                await self._check_daily_tasks()
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                await asyncio.sleep(60)

    async def _check_daily_tasks(self):
        now = get_moscow_time()
        if now.hour == 9 and now.minute == 0:
            today = now.date()
            if self._last_daily_report != today:
                self._last_daily_report = today
                await self._send_daily_report()

    async def _send_daily_report(self):
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(User))
                users = result.scalars().all()
                users_count = len(users)
                
                result = await session.execute(select(Project).where(Project.is_active == True))
                projects = result.scalars().all()
                projects_count = len(projects)
                
                result = await session.execute(
                    select(SourceChannel).where(SourceChannel.is_active == True)
                )
                sources = result.scalars().all()
                sources_count = len(sources)
                
                total_parsed = sum(p.posts_parsed_today for p in projects)
                total_posted = sum(p.posts_posted_today for p in projects)
                
                result = await session.execute(
                    select(PostQueue).where(PostQueue.status == "pending")
                )
                pending = len(result.scalars().all())
                
                result = await session.execute(
                    select(PostQueue).where(PostQueue.status == "failed")
                )
                failed = len(result.scalars().all())
                
                sorted_projects = sorted(projects, key=lambda p: p.posts_posted_today, reverse=True)
                top3 = sorted_projects[:3]
            
            now = datetime.utcnow()
            date_str = now.strftime('%d.%m.%Y')
            
            text = f"📊 <b>Отчёт за {date_str}</b>\n\n"
            text += f"👥 Пользователей: {users_count}\n"
            text += f"📁 Проектов: {projects_count}\n"
            text += f"📥 Источников: {sources_count}\n"
            text += f"🔄 Спарсено сегодня: {total_parsed}\n"
            text += f"📤 Опубликовано сегодня: {total_posted}\n"
            text += f"📬 В очереди: {pending}\n"
            text += f"❌ Ошибок публикации: {failed}\n"
            
            if top3:
                text += f"\n🏆 <b>Топ-{len(top3)} активных проекта:</b>\n"
                for p in top3:
                    text += f"• «{p.name}» — {p.posts_posted_today} постов\n"
            
            from telegram import Bot
            bot = Bot(token=Config.BOT_TOKEN)
            await bot.send_message(
                chat_id=Config.ADMIN_ID,
                text=text,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Daily report failed: {e}")

    async def _check_projects(self):
        now = datetime.utcnow()
        
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Project).where(Project.is_active == True))
            projects = result.scalars().all()
        
        for project in projects:
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(User).where(User.telegram_id == project.user_id))
                user = result.scalar_one_or_none()
                if not user:
                    continue
                
                if not user.is_admin:
                    has_access = False
                    if user.subscription_active and user.subscription_ends_at and user.subscription_ends_at > now:
                        has_access = True
                    elif user.trial_ends_at and user.trial_ends_at > now:
                        has_access = True
                    if not has_access:
                        continue
                
                interval = project.check_interval_minutes
                if not user.is_admin:
                    interval = max(interval, user.min_check_interval_minutes)
                
                last_check = self._last_check.get(project.id)
                if last_check:
                    elapsed = (now - last_check).total_seconds() / 60
                    if elapsed < interval:
                        continue
                
                self._last_check[project.id] = now
                
                task_key = f"project_{project.id}"
                if task_key not in self._tasks or self._tasks[task_key].done():
                    task = asyncio.create_task(self._process_project(project))
                    self._tasks[task_key] = task
                    logger.info(f"⏰ Project '{project.name}' (ID: {project.id}) scheduled")

    async def _download_media_with_retry(self, scraper, media_url: str, save_path: str, max_retries: int = 3) -> bool:
        """Скачивание медиа с повторными попытками (для Telegram)."""
        for attempt in range(max_retries):
            if await scraper.download_media(media_url, save_path):
                try:
                    file_size = os.path.getsize(save_path)
                    if file_size < 1000:
                        logger.warning(f"Downloaded file too small: {file_size} bytes (attempt {attempt + 1})")
                        os.remove(save_path)
                        if attempt < max_retries - 1:
                            await asyncio.sleep(3)
                            continue
                        return False
                    logger.info(f"✅ Media downloaded: {save_path} ({file_size} bytes)")
                    return True
                except Exception as e:
                    logger.warning(f"File check failed: {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(3)
                        continue
                    return False
            else:
                logger.warning(f"Download attempt {attempt + 1} failed for {media_url}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(3)
        return False

    async def _download_youtube_video(self, video_url: str, save_path: str, quality: str = "720p", max_retries: int = 3) -> bool:
        """Скачивание YouTube видео с повторными попытками."""
        for attempt in range(max_retries):
            result = await self.youtube_scraper.download_video(video_url, save_path, quality)
            if result:
                try:
                    file_size = os.path.getsize(save_path)
                    if file_size < 10240:  # меньше 10KB
                        logger.warning(f"Downloaded file too small: {file_size} bytes (attempt {attempt + 1})")
                        os.remove(save_path)
                        if attempt < max_retries - 1:
                            await asyncio.sleep(3)
                            continue
                        return False
                    logger.info(f"✅ YouTube video downloaded: {save_path} ({file_size / 1024 / 1024:.2f} MB)")
                    return True
                except Exception as e:
                    logger.warning(f"File check failed: {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(3)
                        continue
                    return False
            else:
                logger.warning(f"YouTube download attempt {attempt + 1} failed for {video_url}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5)
        return False

    async def _process_project(self, project: Project):
        logger.info(f"🔍 Processing project '{project.name}' (ID: {project.id})")
        
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == project.user_id))
            user = result.scalar_one_or_none()
            if not user:
                return
            
            if not user.is_admin:
                has_access = False
                now = datetime.utcnow()
                if user.subscription_active and user.subscription_ends_at and user.subscription_ends_at > now:
                    has_access = True
                elif user.trial_ends_at and user.trial_ends_at > now:
                    has_access = True
                if not has_access:
                    return
        
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(SourceChannel).where(SourceChannel.project_id == project.id, SourceChannel.is_active == True)
            )
            sources = result.scalars().all()
            
            result = await session.execute(
                select(TargetChannel).where(TargetChannel.project_id == project.id, TargetChannel.is_active == True)
            )
            target = result.scalar_one_or_none()
        
        if not sources or not target:
            logger.warning(f"⚠️ Project '{project.name}' has no sources or target")
            return
        
        logger.info(f"📊 Project '{project.name}': {len(sources)} sources → {target.channel_title or '—'}")
        
        posts_to_publish = []
        total_parsed = 0
        
        for source in sources:
            if source.source_type == "telegram":
                # Telegram источник
                await self._process_telegram_source(source, project, posts_to_publish, total_parsed)
            else:
                # YouTube источник
                await self._process_youtube_source(source, project, posts_to_publish, total_parsed)
        
        if posts_to_publish:
            logger.info(f"📤 Found {len(posts_to_publish)} posts to queue")
            
            msk_now = get_moscow_time().replace(tzinfo=None)
            
            for i, post in enumerate(posts_to_publish):
                if i == 0:
                    interval_minutes = max(int(project.post_interval_hours * 60), user.min_post_interval_minutes, Config.MIN_POST_INTERVAL_MINUTES)
                    start_hour = project.active_hours_start
                    end_hour = project.active_hours_end
                    
                    minutes_since_start = (msk_now.hour - start_hour) * 60 + msk_now.minute
                    if minutes_since_start < 0:
                        next_time = msk_now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
                    else:
                        slots = (minutes_since_start + interval_minutes - 1) // interval_minutes
                        next_time = msk_now.replace(hour=start_hour, minute=0, second=0, microsecond=0) + timedelta(minutes=slots * interval_minutes)
                    
                    if next_time.hour >= end_hour:
                        next_time = next_time.replace(hour=start_hour, minute=0, second=0, microsecond=0) + timedelta(days=1)
                else:
                    interval_minutes = max(int(project.post_interval_hours * 60), user.min_post_interval_minutes, Config.MIN_POST_INTERVAL_MINUTES)
                    next_time = next_time + timedelta(minutes=interval_minutes)
                    if next_time.hour >= project.active_hours_end:
                        next_time = next_time.replace(hour=project.active_hours_start, minute=0, second=0, microsecond=0) + timedelta(days=1)
                
                utc_time = next_time - timedelta(hours=3)
                
                await self.poster.add_to_queue(
                    project_id=project.id,
                    target_channel_id=target.id,
                    post_data=post,
                    scheduled_time=utc_time,
                    platform=target.platform
                )
                logger.info(f"📅 Post {i+1} scheduled for {next_time.strftime('%d.%m.%Y %H:%M')} MSK")
            
            async with AsyncSessionLocal() as session:
                result = await session.execute(select(Project).where(Project.id == project.id))
                db_project = result.scalar_one()
                today = datetime.utcnow().date()
                if db_project.last_reset.date() < today:
                    db_project.posts_parsed_today = 0
                    db_project.posts_posted_today = 0
                    db_project.last_reset = datetime.utcnow()
                db_project.posts_parsed_today += total_parsed
                await session.commit()
        
        logger.info(f"✅ Project '{project.name}' processing completed")

    async def _process_telegram_source(self, source, project, posts_to_publish, total_parsed):
        """Обработка Telegram источника."""
        async with TelegramScraper() as scraper:
            logger.info(f"📡 Fetching @{source.channel_username}")
            
            try:
                posts = await scraper.get_posts(source.channel_username, limit=100)
                logger.info(f"📨 @{source.channel_username}: {len(posts)} posts fetched")
            except Exception as e:
                logger.error(f"❌ Failed to fetch @{source.channel_username}: {e}")
                return
            
            best_post = None
            best_score = -1
            
            for post in posts:
                if await is_post_parsed(project.id, post["url"]):
                    continue
                
                if post.get("is_advertisement", False):
                    continue
                
                # Проверка возраста поста
                if source.max_age_hours and source.max_age_hours > 0:
                    if post.get("datetime"):
                        try:
                            post_time = datetime.fromisoformat(post["datetime"].replace("Z", "+00:00"))
                            age_hours = (datetime.utcnow() - post_time).total_seconds() / 3600
                            if age_hours > source.max_age_hours:
                                continue
                        except:
                            pass
                
                # Проверка ключевых слов
                if source.include_keywords:
                    keywords = [k.strip().lower() for k in source.include_keywords.split(",") if k.strip()]
                    post_text = post.get("text", "").lower()
                    if not any(keyword in post_text for keyword in keywords):
                        continue
                
                media_type = post.get("media_type")
                has_media = post.get("has_media", False)
                
                if source.media_filter == "photo_only":
                    if not has_media or media_type != "photo":
                        continue
                elif source.media_filter == "video_only":
                    if not has_media or media_type != "video":
                        continue
                
                post["source_username"] = source.channel_username
                post["source_title"] = source.channel_title
                post["media_filter"] = source.media_filter
                post["remove_original_text"] = source.remove_original_text
                post["max_video_duration"] = source.max_video_duration
                post["exclude_phrases"] = source.exclude_phrases
                
                post_time = datetime.utcnow()
                if post.get("datetime"):
                    try:
                        post_time = datetime.fromisoformat(post["datetime"].replace("Z", "+00:00"))
                    except:
                        pass
                
                score, is_fallback = calculate_score(post, source.criteria, post_time)
                
                if is_fallback:
                    continue
                
                if score > best_score:
                    best_score = score
                    best_post = post
            
            if best_post:
                if source.max_video_duration and source.max_video_duration > 0:
                    video_dur = best_post.get("video_duration", 0)
                    if video_dur > 0 and video_dur > source.max_video_duration:
                        logger.info(f"⏰ Video too long from @{source.channel_username}: {video_dur}s > {source.max_video_duration}s")
                        return
                
                media_type = best_post.get("media_type")
                has_media = best_post.get("has_media", False)
                
                if source.media_filter == "photo_only":
                    if not has_media or media_type != "photo":
                        return
                elif source.media_filter == "video_only":
                    if not has_media or media_type != "video":
                        return
                
                logger.info(f"🏆 Selected from @{source.channel_username}: score={best_score}, type={media_type}")
                
                await mark_post_parsed(project.id, source.id, best_post["url"])
                total_parsed += 1
                
                media_downloaded = False
                if best_post.get("has_media") and best_post.get("media_url"):
                    ext = "jpg" if best_post.get("media_type") == "photo" else "mp4"
                    filename = f"{uuid.uuid4()}.{ext}"
                    media_path = os.path.join(Config.TEMP_DIR, filename)
                    
                    if await self._download_media_with_retry(scraper, best_post["media_url"], media_path):
                        best_post["media_path"] = media_path
                        media_downloaded = True
                        logger.info(f"💾 Media saved: {media_path}")
                
                if source.media_filter in ("photo_only", "video_only") and not media_downloaded:
                    return
                
                if source.remove_original_text and not media_downloaded:
                    return
                
                has_text = bool(best_post.get("text", "").strip())
                if not has_text and not media_downloaded:
                    return
                
                posts_to_publish.append(best_post)
                
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        update(SourceChannel)
                        .where(SourceChannel.id == source.id)
                        .values(last_parsed=datetime.utcnow(), last_post_url=best_post["url"])
                    )
                    await session.commit()
            else:
                logger.info(f"😴 @{source.channel_username}: no suitable posts")

    async def _process_youtube_source(self, source, project, posts_to_publish, total_parsed):
        """Обработка YouTube источника."""
        logger.info(f"🎬 Fetching YouTube videos for source ID {source.id}")
        
        try:
            # Поиск видео
            videos = await self.youtube_scraper.search_videos(
                query=source.youtube_query,
                category_id=source.youtube_category,
                region_code=source.youtube_region,
                max_results=source.youtube_max_results or 10,
                video_duration="any"
            )
            
            logger.info(f"📨 YouTube: found {len(videos)} videos")
            
            # Фильтрация по критериям
            filtered_videos = []
            for video in videos:
                # Проверка возраста
                if source.max_age_hours and source.max_age_hours > 0:
                    if video.get("published_at"):
                        try:
                            published_at = datetime.fromisoformat(video["published_at"].replace("Z", "+00:00"))
                            age_hours = (datetime.utcnow() - published_at).total_seconds() / 3600
                            if age_hours > source.max_age_hours:
                                continue
                        except:
                            pass
                
                # Проверка просмотров
                if source.min_views and source.min_views > 0:
                    if video.get("views", 0) < source.min_views:
                        continue
                
                # Проверка лайков
                if source.min_likes and source.min_likes > 0:
                    if video.get("likes", 0) < source.min_likes:
                        continue
                
                # Проверка комментариев
                if source.min_comments and source.min_comments > 0:
                    if video.get("comments", 0) < source.min_comments:
                        continue
                
                # Проверка ключевых слов
                if source.include_keywords:
                    keywords = [k.strip().lower() for k in source.include_keywords.split(",") if k.strip()]
                    video_text = (video.get("title", "") + " " + video.get("description", "")).lower()
                    if not any(keyword in video_text for keyword in keywords):
                        continue
                
                # Проверка Shorts
                if source.media_filter == "shorts_only":
                    if not video.get("is_short", False):
                        continue
                
                # Проверка длительности
                if source.max_video_duration and source.max_video_duration > 0:
                    if video.get("duration_seconds", 0) > source.max_video_duration:
                        continue
                
                filtered_videos.append(video)
            
            # Сортируем по просмотрам (убывание)
            filtered_videos.sort(key=lambda x: x.get("views", 0), reverse=True)
            
            # Берём топ-1 видео
            if filtered_videos:
                best_video = filtered_videos[0]
                
                # Проверяем, не парсили ли уже это видео
                if await is_post_parsed(project.id, best_video["url"]):
                    logger.info(f"⏭️ Video already parsed: {best_video['url']}")
                    return
                
                logger.info(f"🏆 Selected YouTube video: {best_video.get('title')} (views: {best_video.get('views')})")
                
                # Скачиваем видео
                filename = f"{uuid.uuid4()}.mp4"
                media_path = os.path.join(Config.TEMP_DIR, filename)
                
                quality = source.video_quality or "720p"
                
                if await self._download_youtube_video(best_video["url"], media_path, quality):
                    best_video["media_path"] = media_path
                    best_video["source_username"] = best_video.get("channel_title", "YouTube")
                    best_video["source_title"] = best_video.get("channel_title", "YouTube")
                    best_video["remove_original_text"] = source.remove_original_text
                    
                    # Формируем текст поста
                    text = best_video.get("description", "")
                    if source.remove_original_text:
                        text = ""
                    
                    best_video["text"] = text
                    best_video["url"] = best_video["url"]
                    best_video["views"] = best_video.get("views", 0)
                    best_video["reactions"] = best_video.get("likes", 0)
                    
                    await mark_post_parsed(project.id, source.id, best_video["url"])
                    total_parsed += 1
                    
                    posts_to_publish.append(best_video)
                    
                    async with AsyncSessionLocal() as session:
                        await session.execute(
                            update(SourceChannel)
                            .where(SourceChannel.id == source.id)
                            .values(last_parsed=datetime.utcnow(), last_post_url=best_video["url"])
                        )
                        await session.commit()
                    
                    logger.info(f"💾 YouTube video saved: {media_path}")
                else:
                    logger.error(f"❌ Failed to download YouTube video: {best_video['url']}")
            else:
                logger.info(f"😴 No suitable YouTube videos found for source ID {source.id}")
                
        except Exception as e:
            logger.error(f"❌ Error processing YouTube source {source.id}: {e}")

    async def stop(self):
        self._running = False
        for task_key, task in self._tasks.items():
            if not task.done():
                task.cancel()
        logger.info("🔴 Scheduler stopped")