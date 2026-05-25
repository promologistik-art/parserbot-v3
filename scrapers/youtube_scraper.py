import asyncio
import logging
import re
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import yt_dlp
import os

from config import Config

logger = logging.getLogger(__name__)

# Категории YouTube (полный список)
YOUTUBE_CATEGORIES = {
    "1": "Film & Animation",
    "2": "Autos & Vehicles",
    "10": "Music",
    "15": "Pets & Animals",
    "17": "Sports",
    "18": "Short Movies",
    "19": "Travel & Events",
    "20": "Gaming",
    "21": "Videoblogging",
    "22": "People & Blogs",
    "23": "Comedy",
    "24": "Entertainment",
    "25": "News & Politics",
    "26": "Howto & Style",
    "27": "Education",
    "28": "Science & Technology",
    "29": "Nonprofits & Activism",
    "30": "Movies",
    "31": "Anime/Animation",
    "32": "Action/Adventure",
    "33": "Classics",
    "34": "Comedy",
    "35": "Documentary",
    "36": "Drama",
    "37": "Family",
    "38": "Foreign",
    "39": "Horror",
    "40": "Sci-Fi/Fantasy",
    "41": "Thriller",
    "42": "Shorts",
    "43": "Shows",
    "44": "Trailers"
}

# Регионы для поиска
YOUTUBE_REGIONS = {
    "RU": "Russia",
    "US": "United States",
    "GB": "United Kingdom",
    "DE": "Germany",
    "FR": "France",
    "ES": "Spain",
    "IT": "Italy",
    "CA": "Canada",
    "AU": "Australia",
    "IN": "India",
    "BR": "Brazil",
    "MX": "Mexico",
    "JP": "Japan",
    "KR": "South Korea"
}


class YouTubeScraper:
    """Класс для работы с YouTube API и скачивания видео."""
    
    def __init__(self):
        self.api_key = Config.YOUTUBE_API_KEY
        self.youtube = None
        if self.api_key:
            try:
                self.youtube = build("youtube", "v3", developerKey=self.api_key)
                logger.info("✅ YouTube API initialized")
            except Exception as e:
                logger.error(f"Failed to initialize YouTube API: {e}")
                self.youtube = None
        else:
            logger.warning("⚠️ YOUTUBE_API_KEY not set. YouTube features will not work.")

    async def search_videos(
        self,
        query: str = None,
        category_id: str = None,
        region_code: str = None,
        max_results: int = 10,
        video_duration: str = "short"  # "short", "medium", "long", "any"
    ) -> List[Dict]:
        """
        Поиск видео на YouTube.
        
        Args:
            query: поисковый запрос
            category_id: ID категории
            region_code: код региона (RU, US, etc.)
            max_results: максимальное количество результатов
            video_duration: длительность видео (short - до 4 мин, medium - до 20 мин, long - больше 20 мин)
        
        Returns:
            Список видео с метаданными
        """
        if not self.youtube:
            logger.error("YouTube API not available")
            return []
        
        try:
            search_params = {
                "part": "snippet",
                "type": "video",
                "maxResults": min(max_results, 50),
                "order": "viewCount",
                "videoDuration": video_duration,
            }
            
            if query:
                search_params["q"] = query
            if category_id:
                search_params["videoCategoryId"] = category_id
            if region_code:
                search_params["regionCode"] = region_code
            
            # Выполняем поиск в отдельном потоке (API синхронный)
            loop = asyncio.get_event_loop()
            search_response = await loop.run_in_executor(
                None, 
                lambda: self.youtube.search().list(**search_params).execute()
            )
            
            video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]
            
            if not video_ids:
                return []
            
            # Получаем подробную информацию о видео
            videos_response = await loop.run_in_executor(
                None,
                lambda: self.youtube.videos().list(
                    part="snippet,statistics,contentDetails",
                    id=",".join(video_ids)
                ).execute()
            )
            
            results = []
            for video in videos_response.get("items", []):
                video_data = self._parse_video_data(video)
                results.append(video_data)
            
            # Сортируем по просмотрам (убывание)
            results.sort(key=lambda x: x.get("views", 0), reverse=True)
            
            logger.info(f"✅ Found {len(results)} videos for query: {query or category_id or region_code}")
            return results[:max_results]
            
        except HttpError as e:
            logger.error(f"YouTube API error: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error in search_videos: {e}")
            return []

    async def get_video_info(self, video_url: str) -> Optional[Dict]:
        """
        Получение информации о видео по URL.
        
        Args:
            video_url: ссылка на видео (обычное, shorts, etc.)
        
        Returns:
            Словарь с информацией о видео
        """
        # Извлекаем video_id из URL
        video_id = self._extract_video_id(video_url)
        if not video_id:
            logger.error(f"Failed to extract video ID from URL: {video_url}")
            return None
        
        if not self.youtube:
            logger.error("YouTube API not available")
            return None
        
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.youtube.videos().list(
                    part="snippet,statistics,contentDetails",
                    id=video_id
                ).execute()
            )
            
            items = response.get("items", [])
            if not items:
                return None
            
            return self._parse_video_data(items[0])
            
        except HttpError as e:
            logger.error(f"YouTube API error: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in get_video_info: {e}")
            return None

    async def download_video(
        self,
        video_url: str,
        save_path: str,
        quality: str = "720p",
        max_size_mb: int = 50
    ) -> Optional[str]:
        """
        Скачивание видео с YouTube.
        
        Args:
            video_url: ссылка на видео
            save_path: путь для сохранения
            quality: качество видео (480p, 720p, 1080p)
            max_size_mb: максимальный размер файла в MB
        
        Returns:
            Путь к сохранённому файлу или None при ошибке
        """
        quality_map = {
            "480p": "best[height<=480]",
            "720p": "best[height<=720]",
            "1080p": "best[height<=1080]"
        }
        
        format_spec = quality_map.get(quality, "best[height<=720]")
        
        ydl_opts = {
            'format': f'{format_spec}[ext=mp4]/best[ext=mp4]/best',
            'outtmpl': save_path,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'force_generic_extractor': False,
            'nocheckcertificate': True,
            'ignoreerrors': True,
            'logtostderr': False,
            'socket_timeout': 30,
            'retries': 3,
            'fragment_retries': 3,
            'headers': {
                'User-Agent': Config.SCRAPER_USER_AGENT,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-us,en;q=0.5',
                'Sec-Fetch-Mode': 'navigate',
            }
        }
        
        try:
            loop = asyncio.get_event_loop()
            
            def download():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([video_url])
                    # Проверяем размер файла
                    if os.path.exists(save_path):
                        file_size = os.path.getsize(save_path)
                        file_size_mb = file_size / (1024 * 1024)
                        if file_size_mb > max_size_mb:
                            logger.warning(f"Video too large: {file_size_mb:.1f}MB > {max_size_mb}MB")
                            os.remove(save_path)
                            return None
                        return save_path
                    return None
            
            result = await loop.run_in_executor(None, download)
            
            if result:
                logger.info(f"✅ Downloaded video to {save_path}")
                return result
            else:
                logger.error(f"Failed to download video from {video_url}")
                return None
                
        except Exception as e:
            logger.error(f"Download error: {e}")
            return None

    def _extract_video_id(self, url: str) -> Optional[str]:
        """Извлекает video ID из URL YouTube."""
        patterns = [
            r'(?:youtube\.com\/watch\?v=)([\w-]+)',
            r'(?:youtu\.be\/)([\w-]+)',
            r'(?:youtube\.com\/shorts\/)([\w-]+)',
            r'(?:youtube\.com\/embed\/)([\w-]+)',
            r'(?:youtube\.com\/v\/)([\w-]+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        return None

    def _parse_video_data(self, video: Dict) -> Dict:
        """Парсит данные видео из ответа API."""
        snippet = video.get("snippet", {})
        statistics = video.get("statistics", {})
        content_details = video.get("contentDetails", {})
        
        # Парсим длительность (формат PT1H2M3S)
        duration_str = content_details.get("duration", "PT0S")
        duration_seconds = self._parse_duration(duration_str)
        
        # Определяем тип видео
        is_short = duration_seconds <= 60
        
        return {
            "url": f"https://youtube.com/watch?v={video.get('id')}",
            "video_id": video.get("id"),
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "channel_title": snippet.get("channelTitle", ""),
            "channel_id": snippet.get("channelId", ""),
            "published_at": snippet.get("publishedAt", ""),
            "views": int(statistics.get("viewCount", 0)),
            "likes": int(statistics.get("likeCount", 0)),
            "comments": int(statistics.get("commentCount", 0)),
            "duration_seconds": duration_seconds,
            "duration_str": duration_str,
            "is_short": is_short,
            "thumbnail_url": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
            "category_id": snippet.get("categoryId", ""),
            "tags": snippet.get("tags", []),
            "source_type": "youtube"
        }

    def _parse_duration(self, duration_str: str) -> int:
        """Парсит длительность из формата ISO 8601 в секунды."""
        pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
        match = re.match(pattern, duration_str)
        if not match:
            return 0
        
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        
        return hours * 3600 + minutes * 60 + seconds


# Список доступных категорий для отображения пользователю
def get_categories_list() -> List[Dict]:
    """Возвращает список категорий для выбора пользователем."""
    return [{"id": k, "name": v} for k, v in YOUTUBE_CATEGORIES.items()]


def get_regions_list() -> List[Dict]:
    """Возвращает список регионов для выбора пользователем."""
    return [{"code": k, "name": v} for k, v in YOUTUBE_REGIONS.items()]