import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy import select, update as sql_update, delete
from database import AsyncSessionLocal
from models import User, SourceChannel
from scrapers import TelegramScraper
from scrapers.youtube_scraper import YouTubeScraper, get_categories_list, get_regions_list
from utils import extract_channel_username
from .utils import require_project, get_sources_count, get_project_target, send_project_ready_message, check_action_limit, check_user_access
from .constants import (
    AWAITING_SOURCE_USERNAME, AWAITING_TARGET_FORWARD, AWAITING_CRITERIA,
    AWAITING_INTERVAL, AWAITING_VIEWS, AWAITING_REACTIONS, AWAITING_SIGNATURE,
    AWAITING_POST_INTERVAL, AWAITING_POST_START_TIME,
    AWAITING_MEDIA_FILTER, AWAITING_REMOVE_TEXT, CURRENT_PROJECT_KEY,
    AWAITING_EDIT_VIEWS, AWAITING_EDIT_REACTIONS, AWAITING_EDIT_EXCLUDE_PHRASES,
    AWAITING_KEYWORDS, AWAITING_EDIT_KEYWORDS,
    AWAITING_SOURCE_TYPE, AWAITING_YOUTUBE_QUERY, AWAITING_YOUTUBE_CATEGORY,
    AWAITING_YOUTUBE_REGION, AWAITING_YOUTUBE_CRITERIA
)

logger = logging.getLogger(__name__)


# ============ ДОБАВЛЕНИЕ ИСТОЧНИКА ============

async def add_source_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("=" * 50)
    logger.info("add_source_start CALLED")
    logger.info(f"User ID: {update.effective_user.id}")
    logger.info(f"user_data BEFORE clear: {context.user_data}")
    
    current_project = context.user_data.get(CURRENT_PROJECT_KEY)
    context.user_data.clear()
    if current_project:
        context.user_data[CURRENT_PROJECT_KEY] = current_project
    
    logger.info(f"user_data AFTER clear: {context.user_data}")
    
    telegram_id = update.effective_user.id
    logger.info(f"Calling require_project for user {telegram_id}")
    project = await require_project(update, context)
    
    logger.info(f"require_project returned: {project}")
    
    if not project:
        logger.error("No project found, returning ConversationHandler.END")
        return ConversationHandler.END
    
    logger.info(f"Project found: {project.id} - {project.name}")
    
    has_access, message, user = await check_user_access(telegram_id)
    logger.info(f"check_user_access: has_access={has_access}, message={message}")
    
    if not has_access:
        await update.message.reply_text(message)
        return ConversationHandler.END
    
    logger.info(f"check_action_limit for add_source")
    can_add, limit_msg = await check_action_limit(user, "add_source", project_id=project.id)
    logger.info(f"can_add={can_add}, limit_msg={limit_msg}")
    
    if not can_add and not user.is_admin:
        await update.message.reply_text(f"❌ {limit_msg}")
        return ConversationHandler.END
    
    context.user_data['temp_project_id'] = project.id
    context.user_data['temp_project_name'] = project.name
    
    logger.info(f"temp_project_id set to {project.id}")
    
    keyboard = [
        [InlineKeyboardButton("📡 Telegram канал", callback_data="source_type_telegram")],
        [InlineKeyboardButton("🎬 YouTube (поиск)", callback_data="source_type_youtube")],
    ]
    
    await update.message.reply_text(
        f"📥 Добавление источника в «{project.name}»\n\n"
        "Выберите тип источника:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    logger.info("Sent source type selection keyboard")
    logger.info("Returning AWAITING_SOURCE_TYPE")
    return AWAITING_SOURCE_TYPE


async def source_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logger.info(f"source_type_callback called with data: {query.data}")
    await query.answer()
    
    source_type = query.data.replace("source_type_", "")
    context.user_data['temp_source_type'] = source_type
    
    if source_type == "telegram":
        await query.edit_message_text(
            "📡 <b>Telegram канал</b>\n\n"
            "Отправьте username канала (@name) или ссылку:\n"
            "• @durov\n"
            "• https://t.me/durov",
            parse_mode="HTML"
        )
        return AWAITING_SOURCE_USERNAME
    else:
        await query.edit_message_text(
            "🎬 <b>YouTube источник</b>\n\n"
            "Введите поисковый запрос (ключевые слова):\n"
            "Пример: <code>смешные животные</code>\n\n"
            "Или отправьте <code>-</code> чтобы пропустить (будут популярные видео)",
            parse_mode="HTML"
        )
        return AWAITING_YOUTUBE_QUERY


async def add_source_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"add_source_username called with: {update.message.text}")
    username = extract_channel_username(update.message.text)
    if not username:
        await update.message.reply_text("❌ Не удалось распознать username.")
        return AWAITING_SOURCE_USERNAME
    
    async with TelegramScraper() as scraper:
        info = await scraper.get_channel_info(username)
    
    if not info:
        await update.message.reply_text("❌ Канал не найден или не является публичным.")
        return AWAITING_SOURCE_USERNAME
    
    context.user_data['temp_source'] = {
        'username': username,
        'title': info['title'],
        'project_id': context.user_data.get('temp_project_id'),
        'project_name': context.user_data.get('temp_project_name'),
        'source_type': 'telegram'
    }
    
    keyboard = [
        [InlineKeyboardButton("🎯 Свои критерии", callback_data="criteria_custom")],
        [InlineKeyboardButton("👁 1000+ просмотров", callback_data="criteria_views")],
        [InlineKeyboardButton("❤️ 50+ реакций", callback_data="criteria_reactions")],
        [InlineKeyboardButton("👁+❤️ 500+ и 25+", callback_data="criteria_both")],
        [InlineKeyboardButton("⚡ Без критериев", callback_data="criteria_none")],
    ]
    
    await update.message.reply_text(
        f"✅ Канал: @{username}\n📝 Название: {info['title']}\n\nВыберите критерии отбора:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return AWAITING_CRITERIA


async def add_source_criteria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("criteria_", "")
    temp = context.user_data.get('temp_source')
    
    if not temp:
        await query.edit_message_text("❌ Ошибка: данные не найдены.")
        return ConversationHandler.END
    
    if choice == "custom":
        await query.edit_message_text(
            "📊 <b>Настройка критериев</b>\n\nВведите минимальное количество просмотров (0 = не учитывать):",
            parse_mode="HTML"
        )
        context.user_data['awaiting_criteria'] = 'views'
        return AWAITING_VIEWS
    else:
        criteria = {
            "views": {"min_views": 1000},
            "reactions": {"min_reactions": 50},
            "both": {"min_views": 500, "min_reactions": 25},
            "none": {}
        }.get(choice, {})
        
        context.user_data['temp_criteria'] = criteria
        
        keyboard = [
            [InlineKeyboardButton("📷 Все (фото + видео)", callback_data="media_all")],
            [InlineKeyboardButton("🖼️ Только фото", callback_data="media_photo_only")],
            [InlineKeyboardButton("🎬 Только видео", callback_data="media_video_only")],
        ]
        
        await query.edit_message_text(
            f"✅ Критерии выбраны\n\nТеперь выберите тип контента для @{temp['username']}:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return AWAITING_MEDIA_FILTER


async def youtube_query_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("=" * 50)
    logger.info("youtube_query_input CALLED")
    logger.info(f"Message text: {update.message.text}")
    logger.info("=" * 50)
    
    query_text = update.message.text.strip()
    
    if query_text == "-":
        context.user_data['temp_youtube_query'] = None
    else:
        context.user_data['temp_youtube_query'] = query_text
    
    categories = get_categories_list()
    keyboard = []
    row = []
    for cat in categories[:20]:
        row.append(InlineKeyboardButton(cat['name'][:20], callback_data=f"youtube_cat_{cat['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🌍 Все категории", callback_data="youtube_cat_all")])
    
    await update.message.reply_text(
        "🎬 <b>Выберите категорию видео</b>\n\n"
        "Выберите категорию для поиска:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return AWAITING_YOUTUBE_CATEGORY


async def youtube_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    cat_data = query.data.replace("youtube_cat_", "")
    if cat_data == "all":
        context.user_data['temp_youtube_category'] = None
    else:
        context.user_data['temp_youtube_category'] = cat_data
    
    regions = get_regions_list()
    keyboard = []
    row = []
    for region in regions:
        row.append(InlineKeyboardButton(region['name'][:15], callback_data=f"youtube_region_{region['code']}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🌍 Все регионы", callback_data="youtube_region_all")])
    
    await query.edit_message_text(
        "🌍 <b>Выберите регион</b>\n\n"
        "Выберите регион для поиска:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return AWAITING_YOUTUBE_REGION


async def youtube_region_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    region_data = query.data.replace("youtube_region_", "")
    if region_data == "all":
        context.user_data['temp_youtube_region'] = None
    else:
        context.user_data['temp_youtube_region'] = region_data
    
    keyboard = [
        [InlineKeyboardButton("🎯 Свои критерии", callback_data="youtube_criteria_custom")],
        [InlineKeyboardButton("👁 100K+ просмотров", callback_data="youtube_criteria_views")],
        [InlineKeyboardButton("👍 1K+ лайков", callback_data="youtube_criteria_likes")],
        [InlineKeyboardButton("💬 100+ комментариев", callback_data="youtube_criteria_comments")],
        [InlineKeyboardButton("⭐ Популярное", callback_data="youtube_criteria_popular")],
        [InlineKeyboardButton("⚡ Без критериев", callback_data="youtube_criteria_none")],
    ]
    
    await query.edit_message_text(
        "📊 <b>Критерии отбора видео</b>\n\n"
        "Выберите критерии для фильтрации видео:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return AWAITING_YOUTUBE_CRITERIA


async def youtube_criteria_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("youtube_criteria_", "")
    
    if choice == "custom":
        await query.edit_message_text(
            "📊 <b>Настройка критериев</b>\n\n"
            "Введите минимальное количество просмотров (0 = не учитывать):",
            parse_mode="HTML"
        )
        context.user_data['awaiting_youtube_criteria'] = 'views'
        return AWAITING_VIEWS
    else:
        criteria = {
            "views": {"min_views": 100000},
            "likes": {"min_likes": 1000},
            "comments": {"min_comments": 100},
            "popular": {"min_views": 500000, "min_likes": 5000, "min_comments": 500},
            "none": {}
        }.get(choice, {})
        
        context.user_data['temp_youtube_criteria'] = criteria
        
        keyboard = [
            [InlineKeyboardButton("🎬 Все видео", callback_data="media_all")],
            [InlineKeyboardButton("📱 Только Shorts", callback_data="media_shorts_only")],
        ]
        
        await query.edit_message_text(
            f"✅ Критерии выбраны\n\nТеперь выберите тип контента:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return AWAITING_MEDIA_FILTER


async def criteria_views_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        views = int(update.message.text.strip())
        if views < 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите целое число (0 = не учитывать):")
        return AWAITING_VIEWS
    
    context.user_data['temp_criteria_views'] = views
    await update.message.reply_text("📊 Введите минимальное количество реакций (0 = не учитывать):")
    return AWAITING_REACTIONS


async def criteria_reactions_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reactions = int(update.message.text.strip())
        if reactions < 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите целое число (0 = не учитывать):")
        return AWAITING_REACTIONS
    
    views = context.user_data.get('temp_criteria_views', 0)
    criteria = {}
    if views > 0:
        criteria['min_views'] = views
    if reactions > 0:
        criteria['min_reactions'] = reactions
    
    context.user_data['temp_criteria'] = criteria
    
    keyboard = [
        [InlineKeyboardButton("📷 Все (фото + видео)", callback_data="media_all")],
        [InlineKeyboardButton("🖼️ Только фото", callback_data="media_photo_only")],
        [InlineKeyboardButton("🎬 Только видео", callback_data="media_video_only")],
    ]
    
    await update.message.reply_text(
        f"✅ Критерии сохранены\n\nТеперь выберите тип контента:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return AWAITING_MEDIA_FILTER


async def youtube_views_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        views = int(update.message.text.strip())
        if views < 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите целое число (0 = не учитывать):")
        return AWAITING_VIEWS
    
    context.user_data['temp_youtube_min_views'] = views
    await update.message.reply_text("👍 Введите минимальное количество лайков (0 = не учитывать):")
    return AWAITING_REACTIONS


async def youtube_likes_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        likes = int(update.message.text.strip())
        if likes < 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите целое число (0 = не учитывать):")
        return AWAITING_REACTIONS
    
    context.user_data['temp_youtube_min_likes'] = likes
    await update.message.reply_text("💬 Введите минимальное количество комментариев (0 = не учитывать):")
    return AWAITING_VIEWS


async def youtube_comments_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        comments = int(update.message.text.strip())
        if comments < 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите целое число (0 = не учитывать):")
        return AWAITING_VIEWS
    
    criteria = {}
    if context.user_data.get('temp_youtube_min_views', 0) > 0:
        criteria['min_views'] = context.user_data['temp_youtube_min_views']
    if context.user_data.get('temp_youtube_min_likes', 0) > 0:
        criteria['min_likes'] = context.user_data['temp_youtube_min_likes']
    if comments > 0:
        criteria['min_comments'] = comments
    
    context.user_data['temp_youtube_criteria'] = criteria
    
    keyboard = [
        [InlineKeyboardButton("🎬 Все видео", callback_data="media_all")],
        [InlineKeyboardButton("📱 Только Shorts", callback_data="media_shorts_only")],
    ]
    
    await update.message.reply_text(
        f"✅ Критерии сохранены\n\nТеперь выберите тип контента:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return AWAITING_MEDIA_FILTER


async def media_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("media_", "")
    context.user_data['temp_media_filter'] = choice
    
    source_type = context.user_data.get('temp_source_type', 'telegram')
    
    if source_type == "telegram" and choice in ("video_only", "all"):
        keyboard = [
            [InlineKeyboardButton("📏 До 1 минуты", callback_data="duration_60")],
            [InlineKeyboardButton("📏 До 3 минут", callback_data="duration_180")],
            [InlineKeyboardButton("📏 Без ограничений", callback_data="duration_0")],
        ]
        
        await query.edit_message_text(
            f"🎬 <b>Ограничение по длительности видео:</b>\n\nВыберите максимальную длительность:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        context.user_data['awaiting_duration'] = True
        return AWAITING_MEDIA_FILTER
    else:
        context.user_data['temp_max_video_duration'] = None
        return await ask_remove_text(query, context)


async def duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("duration_", "")
    duration = int(choice)
    context.user_data['temp_max_video_duration'] = duration if duration > 0 else None
    
    return await ask_remove_text(query, context)


async def ask_remove_text(target, context):
    keyboard = [
        [InlineKeyboardButton("✅ Оставлять текст", callback_data="text_keep")],
        [InlineKeyboardButton("❌ Удалять текст", callback_data="text_remove")],
    ]
    
    text = (
        f"📝 <b>Оригинальный текст/описание:</b>\n\n"
        f"Хотите оставлять или удалять описание видео?\n"
        f"Если удалить — останется только медиа и подпись."
    )
    
    if hasattr(target, 'edit_message_text'):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await target.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    
    context.user_data['awaiting_text_choice'] = True
    return AWAITING_REMOVE_TEXT


async def remove_text_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("text_", "")
    remove_text = (choice == "remove")
    
    source_type = context.user_data.get('temp_source_type', 'telegram')
    
    if source_type == "telegram":
        temp = context.user_data.get('temp_source')
        criteria = context.user_data.get('temp_criteria', {})
        media_filter = context.user_data.get('temp_media_filter', 'all')
        max_video_duration = context.user_data.get('temp_max_video_duration')
        
        if not temp:
            await query.edit_message_text("❌ Ошибка: данные не найдены.")
            return ConversationHandler.END
        
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(SourceChannel).where(
                    SourceChannel.project_id == temp['project_id'],
                    SourceChannel.channel_username == temp['username']
                )
            )
            if result.scalar_one_or_none():
                await query.edit_message_text(f"⚠️ Канал @{temp['username']} уже добавлен в этот проект.")
                return ConversationHandler.END
            
            channel = SourceChannel(
                project_id=temp['project_id'],
                source_type="telegram",
                channel_username=temp['username'],
                channel_title=temp['title'],
                criteria=criteria,
                media_filter=media_filter,
                remove_original_text=remove_text,
                max_video_duration=max_video_duration,
                max_age_hours=24
            )
            session.add(channel)
            await session.commit()
            context.user_data['temp_source_id'] = channel.id
        
        filter_text = {"all": "все", "photo_only": "только фото", "video_only": "только видео"}.get(media_filter, "все")
        
        criteria_parts = []
        if criteria.get('min_views'):
            criteria_parts.append(f"👁 от {criteria['min_views']}")
        if criteria.get('min_reactions'):
            criteria_parts.append(f"❤️ от {criteria['min_reactions']}")
        criteria_display = ", ".join(criteria_parts) if criteria_parts else "без критериев"
        
        text_parts = [f"✅ Канал @{temp['username']} добавлен!"]
        text_parts.append(f"📋 Критерии: {criteria_display}")
        text_parts.append(f"📷 Контент: {filter_text}")
        if max_video_duration:
            text_parts.append(f"🎬 Длительность видео: до {max_video_duration} сек")
        text_parts.append(f"📝 Текст: {'удаляется' if remove_text else 'оставляется'}")
        
        await query.edit_message_text("\n".join(text_parts))
        
    else:
        project_id = context.user_data.get('temp_project_id')
        youtube_query = context.user_data.get('temp_youtube_query')
        youtube_category = context.user_data.get('temp_youtube_category')
        youtube_region = context.user_data.get('temp_youtube_region')
        youtube_criteria = context.user_data.get('temp_youtube_criteria', {})
        media_filter = context.user_data.get('temp_media_filter', 'all')
        
        async with AsyncSessionLocal() as session:
            channel = SourceChannel(
                project_id=project_id,
                source_type="youtube",
                youtube_query=youtube_query,
                youtube_category=youtube_category,
                youtube_region=youtube_region,
                min_views=youtube_criteria.get('min_views', 0),
                min_likes=youtube_criteria.get('min_likes', 0),
                min_comments=youtube_criteria.get('min_comments', 0),
                media_filter=media_filter,
                remove_original_text=remove_text,
                max_age_hours=24
            )
            session.add(channel)
            await session.commit()
            context.user_data['temp_source_id'] = channel.id
        
        filter_text = "📱 Только Shorts" if media_filter == "shorts_only" else "🎬 Все видео"
        
        text_parts = [f"✅ YouTube источник добавлен!"]
        if youtube_query:
            text_parts.append(f"🔍 Запрос: {youtube_query}")
        if youtube_category:
            text_parts.append(f"📁 Категория: {youtube_category}")
        if youtube_region:
            text_parts.append(f"🌍 Регион: {youtube_region}")
        if youtube_criteria.get('min_views'):
            text_parts.append(f"👁 Просмотров ≥ {youtube_criteria['min_views']}")
        if youtube_criteria.get('min_likes'):
            text_parts.append(f"👍 Лайков ≥ {youtube_criteria['min_likes']}")
        if youtube_criteria.get('min_comments'):
            text_parts.append(f"💬 Комментариев ≥ {youtube_criteria['min_comments']}")
        text_parts.append(f"📷 Контент: {filter_text}")
        text_parts.append(f"📝 Описание: {'удаляется' if remove_text else 'оставляется'}")
        
        await query.edit_message_text("\n".join(text_parts))
    
    keyboard = [
        [InlineKeyboardButton("✅ Добавить ключевые слова", callback_data="add_keywords_yes")],
        [InlineKeyboardButton("⏭️ Пропустить", callback_data="add_keywords_skip")]
    ]
    
    await query.message.reply_text(
        f"🔍 <b>Ключевые слова для фильтрации</b>\n\n"
        f"Вы можете указать ключевые слова (через запятую).\n"
        f"Бот будет публиковать только видео, содержащие эти слова в названии или описании.\n\n"
        f"Если пропустить — будут публиковаться все видео.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return AWAITING_KEYWORDS


async def add_keywords_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "🔍 <b>Введите ключевые слова</b>\n\n"
        "Введите слова или фразы через запятую.\n"
        "Пример: <code>смешные животные, приколы, забавные</code>\n\n"
        "Бот будет публиковать только видео, содержащие хотя бы одно из этих слов.\n\n"
        "Отправьте <code>-</code> чтобы пропустить.",
        parse_mode="HTML"
    )
    return AWAITING_KEYWORDS


async def add_keywords_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    source_id = context.user_data.get('temp_source_id')
    project_id = context.user_data.get('temp_project_id')
    project_name = context.user_data.get('temp_project_name')
    
    await query.edit_message_text("✅ Источник добавлен! Ключевые слова не указаны.")
    
    context.user_data.pop('temp_source_id', None)
    context.user_data.pop('temp_source', None)
    context.user_data.pop('temp_project_id', None)
    context.user_data.pop('temp_project_name', None)
    context.user_data.pop('temp_criteria', None)
    context.user_data.pop('temp_criteria_views', None)
    context.user_data.pop('temp_media_filter', None)
    context.user_data.pop('temp_max_video_duration', None)
    context.user_data.pop('awaiting_criteria', None)
    context.user_data.pop('awaiting_duration', None)
    context.user_data.pop('awaiting_text_choice', None)
    context.user_data.pop('temp_source_type', None)
    context.user_data.pop('temp_youtube_query', None)
    context.user_data.pop('temp_youtube_category', None)
    context.user_data.pop('temp_youtube_region', None)
    context.user_data.pop('temp_youtube_criteria', None)
    context.user_data.pop('temp_youtube_min_views', None)
    context.user_data.pop('temp_youtube_min_likes', None)
    
    sources_count = await get_sources_count(project_id)
    target_channel = await get_project_target(project_id)
    
    if target_channel and sources_count >= 1:
        await query.message.reply_text(
            f"✅ <b>Проект «{project_name}» готов к работе!</b>\n\n"
            f"• /set_interval — настроить частоту парсинга\n"
            f"• /set_post_interval — интервал публикаций\n"
            f"• /set_signature — добавить подпись\n"
            f"• /parse — запустить парсинг\n"
            f"• /add_source — добавить ещё источник",
            parse_mode="HTML"
        )
    
    return ConversationHandler.END


async def process_keywords_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    source_id = context.user_data.get('temp_source_id')
    project_id = context.user_data.get('temp_project_id')
    project_name = context.user_data.get('temp_project_name')
    
    if text == "-":
        keywords = None
        reply = "✅ Источник добавлен! Ключевые слова не указаны."
    else:
        keywords = text
        reply = f"✅ Источник добавлен!\n\n🔍 Ключевые слова: {keywords}"
    
    async with AsyncSessionLocal() as session:
        await session.execute(
            sql_update(SourceChannel)
            .where(SourceChannel.id == source_id)
            .values(include_keywords=keywords)
        )
        await session.commit()
    
    await update.message.reply_text(reply)
    
    context.user_data.pop('temp_source_id', None)
    context.user_data.pop('temp_source', None)
    context.user_data.pop('temp_project_id', None)
    context.user_data.pop('temp_project_name', None)
    context.user_data.pop('temp_criteria', None)
    context.user_data.pop('temp_criteria_views', None)
    context.user_data.pop('temp_media_filter', None)
    context.user_data.pop('temp_max_video_duration', None)
    context.user_data.pop('awaiting_criteria', None)
    context.user_data.pop('awaiting_duration', None)
    context.user_data.pop('awaiting_text_choice', None)
    context.user_data.pop('temp_source_type', None)
    context.user_data.pop('temp_youtube_query', None)
    context.user_data.pop('temp_youtube_category', None)
    context.user_data.pop('temp_youtube_region', None)
    context.user_data.pop('temp_youtube_criteria', None)
    context.user_data.pop('temp_youtube_min_views', None)
    context.user_data.pop('temp_youtube_min_likes', None)
    
    sources_count = await get_sources_count(project_id)
    target_channel = await get_project_target(project_id)
    
    if target_channel and sources_count >= 1:
        await update.message.reply_text(
            f"✅ <b>Проект «{project_name}» готов к работе!</b>\n\n"
            f"• /set_interval — настроить частоту парсинга\n"
            f"• /set_post_interval — интервал публикаций\n"
            f"• /set_signature — добавить подпись\n"
            f"• /parse — запустить парсинг\n"
            f"• /add_source — добавить ещё источник",
            parse_mode="HTML"
        )
    
    return ConversationHandler.END


# ============ РЕДАКТИРОВАНИЕ ИСТОЧНИКА ============

async def edit_source_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    source_id = int(query.data.replace("edit_source_", ""))
    context.user_data['edit_source_id'] = source_id
    
    await show_edit_source_menu(query, source_id)


async def show_edit_source_menu(query, source_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SourceChannel).where(SourceChannel.id == source_id))
        source = result.scalar_one_or_none()
    
    if not source:
        await query.edit_message_text("❌ Источник не найден")
        return
    
    if source.source_type == "telegram":
        filter_names = {"all": "все", "photo_only": "только фото", "video_only": "только видео"}
        
        criteria_parts = []
        if source.criteria:
            if "min_views" in source.criteria:
                criteria_parts.append(f"👁 ≥{source.criteria['min_views']}")
            if "min_reactions" in source.criteria:
                criteria_parts.append(f"❤️ ≥{source.criteria['min_reactions']}")
        criteria_str = ", ".join(criteria_parts) if criteria_parts else "без критериев"
        
        text = (
            f"✏️ <b>Редактирование @{source.channel_username}</b>\n\n"
            f"📊 Критерии: {criteria_str}\n"
            f"📷 Контент: {filter_names.get(source.media_filter, 'все')}\n"
            f"🎬 Длительность видео: {'до ' + str(source.max_video_duration) + 'с' if source.max_video_duration else 'без ограничений'}\n"
            f"📝 Текст: {'удаляется' if source.remove_original_text else 'оставляется'}\n"
            f"🚫 Стоп-фразы: {source.exclude_phrases or 'нет'}\n"
            f"🔍 Ключевые слова: {source.include_keywords or 'не указаны'}\n"
        )
        
        keyboard = [
            [InlineKeyboardButton("📊 Изменить критерии", callback_data=f"edit_criteria_{source_id}")],
            [InlineKeyboardButton("📷 Изменить тип контента", callback_data=f"edit_media_{source_id}")],
            [InlineKeyboardButton("📝 Изменить обработку текста", callback_data=f"edit_text_{source_id}")],
            [InlineKeyboardButton("🚫 Изменить стоп-фразы", callback_data=f"edit_phrases_{source_id}")],
            [InlineKeyboardButton("🔍 Изменить ключевые слова", callback_data=f"edit_keywords_{source_id}")],
            [InlineKeyboardButton("🗑️ Очистить стоп-фразы", callback_data=f"edit_clear_phrases_{source_id}")],
            [InlineKeyboardButton("◀️ Назад к источникам", callback_data="back_to_sources")],
        ]
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        
    else:
        filter_text = "📱 Только Shorts" if source.media_filter == "shorts_only" else "🎬 Все видео"
        
        text = (
            f"✏️ <b>Редактирование YouTube источника</b>\n\n"
            f"🔍 Запрос: {source.youtube_query or 'все'}\n"
            f"📁 Категория: {source.youtube_category or 'все'}\n"
            f"🌍 Регион: {source.youtube_region or 'все'}\n"
            f"👁 Просмотры ≥ {source.min_views or 0}\n"
            f"👍 Лайки ≥ {source.min_likes or 0}\n"
            f"💬 Комментарии ≥ {source.min_comments or 0}\n"
            f"📷 Контент: {filter_text}\n"
            f"📝 Описание: {'удаляется' if source.remove_original_text else 'оставляется'}\n"
            f"🚫 Стоп-фразы: {source.exclude_phrases or 'нет'}\n"
            f"🔍 Ключевые слова: {source.include_keywords or 'не указаны'}\n"
        )
        
        keyboard = [
            [InlineKeyboardButton("📊 Изменить критерии", callback_data=f"edit_youtube_criteria_{source_id}")],
            [InlineKeyboardButton("📷 Изменить тип контента", callback_data=f"edit_media_{source_id}")],
            [InlineKeyboardButton("📝 Изменить обработку текста", callback_data=f"edit_text_{source_id}")],
            [InlineKeyboardButton("🚫 Изменить стоп-фразы", callback_data=f"edit_phrases_{source_id}")],
            [InlineKeyboardButton("🔍 Изменить ключевые слова", callback_data=f"edit_keywords_{source_id}")],
            [InlineKeyboardButton("🗑️ Очистить стоп-фразы", callback_data=f"edit_clear_phrases_{source_id}")],
            [InlineKeyboardButton("◀️ Назад к источникам", callback_data="back_to_sources")],
        ]
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def edit_source_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logger.info(f"🔍 edit_source_start called with data: {query.data}")
    await query.answer()
    
    data = query.data
    
    if data.startswith("edit_clear_phrases_"):
        source_id = int(data.replace("edit_clear_phrases_", ""))
        async with AsyncSessionLocal() as session:
            await session.execute(
                sql_update(SourceChannel)
                .where(SourceChannel.id == source_id)
                .values(exclude_phrases=None)
            )
            await session.commit()
        await show_edit_source_menu(query, source_id)
        return ConversationHandler.END
    
    source_id = int(data.split("_")[-1])
    context.user_data['edit_source_id'] = source_id
    
    if data.startswith("edit_criteria_"):
        await query.edit_message_text(
            "📊 Введите новые минимальные просмотры (0 = не учитывать):"
        )
        return AWAITING_EDIT_VIEWS
    
    elif data.startswith("edit_media_"):
        keyboard = [
            [InlineKeyboardButton("🎬 Все видео", callback_data="edit_media_all")],
            [InlineKeyboardButton("📱 Только Shorts", callback_data="edit_media_shorts_only")],
        ]
        await query.edit_message_text(
            "📷 Выберите новый тип контента:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return AWAITING_MEDIA_FILTER
    
    elif data.startswith("edit_text_"):
        keyboard = [
            [InlineKeyboardButton("✅ Оставлять описание", callback_data="edit_text_keep")],
            [InlineKeyboardButton("❌ Удалять описание", callback_data="edit_text_remove")],
        ]
        await query.edit_message_text(
            "📝 Оставлять или удалять описание видео?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return AWAITING_REMOVE_TEXT
    
    elif data.startswith("edit_phrases_"):
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(SourceChannel).where(SourceChannel.id == source_id))
            source = result.scalar_one()
        
        current = source.exclude_phrases or "нет"
        
        await query.edit_message_text(
            f"🚫 <b>Стоп-фразы</b>\n\n"
            f"Текущие: {current}\n\n"
            f"Введите новые фразы через запятую.\n"
            f"Например: реклама, спонсор, подпишись\n\n"
            f"Новые фразы будут <b>добавлены</b> к существующим.\n"
            f"Для удаления всех фраз нажмите кнопку «Очистить».",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑️ Очистить стоп-фразы", callback_data=f"edit_clear_phrases_{source_id}")
            ]])
        )
        return AWAITING_EDIT_EXCLUDE_PHRASES
    
    elif data.startswith("edit_keywords_"):
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(SourceChannel).where(SourceChannel.id == source_id))
            source = result.scalar_one()
        
        current = source.include_keywords or "не указаны"
        
        await query.edit_message_text(
            f"🔍 <b>Ключевые слова</b>\n\n"
            f"Текущие: {current}\n\n"
            f"Введите новые ключевые слова через запятую.\n"
            f"Пример: <code>смешные животные, приколы</code>\n\n"
            f"Отправьте <code>-</code> чтобы очистить.\n"
            f"/cancel — отмена",
            parse_mode="HTML"
        )
        return AWAITING_EDIT_KEYWORDS
    
    elif data.startswith("edit_youtube_criteria_"):
        await query.edit_message_text(
            "📊 Введите минимальное количество просмотров (0 = не учитывать):"
        )
        return AWAITING_EDIT_VIEWS
    
    return ConversationHandler.END


async def edit_views_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        views = int(update.message.text.strip())
        if views < 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите целое число (0 = не учитывать):")
        return AWAITING_EDIT_VIEWS
    
    context.user_data['edit_views'] = views
    await update.message.reply_text("👍 Введите минимальное количество лайков (0 = не учитывать):")
    return AWAITING_EDIT_REACTIONS


async def edit_reactions_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reactions = int(update.message.text.strip())
        if reactions < 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Введите целое число (0 = не учитывать):")
        return AWAITING_EDIT_REACTIONS
    
    views = context.user_data.get('edit_views', 0)
    
    source_id = context.user_data.get('edit_source_id')
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SourceChannel).where(SourceChannel.id == source_id))
        source = result.scalar_one()
        
        if source.source_type == "telegram":
            criteria = {}
            if views > 0:
                criteria['min_views'] = views
            if reactions > 0:
                criteria['min_reactions'] = reactions
            await session.execute(
                sql_update(SourceChannel)
                .where(SourceChannel.id == source_id)
                .values(criteria=criteria)
            )
        else:
            await session.execute(
                sql_update(SourceChannel)
                .where(SourceChannel.id == source_id)
                .values(min_views=views, min_likes=reactions)
            )
        await session.commit()
    
    await update.message.reply_text("✅ Критерии обновлены!")
    
    class FakeQuery:
        def __init__(self, chat_id, message_id, bot):
            self.message = type('obj', (object,), {
                'chat_id': chat_id,
                'message_id': message_id
            })
            self.bot = bot
        async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
            await self.bot.edit_message_text(
                text=text,
                chat_id=self.message.chat_id,
                message_id=self.message.message_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
        async def answer(self):
            pass
    
    fake_query = FakeQuery(
        update.message.chat_id,
        update.message.message_id - 1,
        context.bot
    )
    
    await show_edit_source_menu(fake_query, source_id)
    
    context.user_data.pop('edit_views', None)
    context.user_data.pop('edit_source_id', None)
    return ConversationHandler.END


async def edit_media_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("edit_media_", "")
    source_id = context.user_data.get('edit_source_id')
    
    async with AsyncSessionLocal() as session:
        await session.execute(
            sql_update(SourceChannel)
            .where(SourceChannel.id == source_id)
            .values(media_filter=choice)
        )
        await session.commit()
    
    filter_text = "📱 Только Shorts" if choice == "shorts_only" else "🎬 Все видео"
    await query.edit_message_text(f"✅ Тип контента обновлён: {filter_text}")
    await show_edit_source_menu(query, source_id)
    
    context.user_data.pop('edit_source_id', None)
    return ConversationHandler.END


async def edit_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("edit_duration_", "")
    duration = int(choice) if int(choice) > 0 else None
    source_id = context.user_data.get('edit_source_id')
    
    async with AsyncSessionLocal() as session:
        await session.execute(
            sql_update(SourceChannel)
            .where(SourceChannel.id == source_id)
            .values(max_video_duration=duration)
        )
        await session.commit()
    
    dur_text = f"до {duration}с" if duration else "без ограничений"
    await query.edit_message_text(f"✅ Длительность видео обновлена: {dur_text}")
    await show_edit_source_menu(query, source_id)
    
    context.user_data.pop('edit_source_id', None)
    return ConversationHandler.END


async def edit_remove_text_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    choice = query.data.replace("edit_text_", "")
    remove_text = (choice == "remove")
    source_id = context.user_data.get('edit_source_id')
    
    async with AsyncSessionLocal() as session:
        await session.execute(
            sql_update(SourceChannel)
            .where(SourceChannel.id == source_id)
            .values(remove_original_text=remove_text)
        )
        await session.commit()
    
    await query.edit_message_text(f"✅ Описание: {'удаляется' if remove_text else 'оставляется'}")
    await show_edit_source_menu(query, source_id)
    
    context.user_data.pop('edit_source_id', None)
    return ConversationHandler.END


async def edit_exclude_phrases_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_phrases_text = update.message.text.strip()
    source_id = context.user_data.get('edit_source_id')
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SourceChannel).where(SourceChannel.id == source_id))
        source = result.scalar_one()
        
        current_phrases = source.exclude_phrases or ""
        
        existing_phrases = [p.strip() for p in current_phrases.split(",") if p.strip()]
        
        if new_phrases_text and new_phrases_text != "-":
            new_phrases = [p.strip() for p in new_phrases_text.split(",") if p.strip()]
            for phrase in new_phrases:
                if phrase not in existing_phrases:
                    existing_phrases.append(phrase)
        
        if existing_phrases:
            updated_phrases = ", ".join(existing_phrases)
        else:
            updated_phrases = None
        
        await session.execute(
            sql_update(SourceChannel)
            .where(SourceChannel.id == source_id)
            .values(exclude_phrases=updated_phrases)
        )
        await session.commit()
    
    if updated_phrases:
        await update.message.reply_text(f"✅ Стоп-фразы обновлены!\nТекущий список: {updated_phrases}")
    else:
        await update.message.reply_text("✅ Все стоп-фразы удалены")
    
    class FakeQuery:
        def __init__(self, chat_id, message_id, bot):
            self.message = type('obj', (object,), {
                'chat_id': chat_id,
                'message_id': message_id
            })
            self.bot = bot
        async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
            await self.bot.edit_message_text(
                text=text,
                chat_id=self.message.chat_id,
                message_id=self.message.message_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
        async def answer(self):
            pass
    
    fake_query = FakeQuery(
        update.message.chat_id,
        update.message.message_id - 1,
        context.bot
    )
    
    await show_edit_source_menu(fake_query, source_id)
    
    context.user_data.pop('edit_source_id', None)
    return ConversationHandler.END


async def edit_keywords_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    source_id = context.user_data.get('edit_source_id')
    
    if text == "-":
        keywords = None
        reply = "✅ Ключевые слова очищены"
    else:
        keywords = text
        reply = f"✅ Ключевые слова обновлены: {keywords}"
    
    async with AsyncSessionLocal() as session:
        await session.execute(
            sql_update(SourceChannel)
            .where(SourceChannel.id == source_id)
            .values(include_keywords=keywords)
        )
        await session.commit()
    
    await update.message.reply_text(reply)
    
    class FakeQuery:
        def __init__(self, chat_id, message_id, bot):
            self.message = type('obj', (object,), {
                'chat_id': chat_id,
                'message_id': message_id
            })
            self.bot = bot
        async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
            await self.bot.edit_message_text(
                text=text,
                chat_id=self.message.chat_id,
                message_id=self.message.message_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
        async def answer(self):
            pass
    
    fake_query = FakeQuery(
        update.message.chat_id,
        update.message.message_id - 1,
        context.bot
    )
    
    await show_edit_source_menu(fake_query, source_id)
    
    context.user_data.pop('edit_source_id', None)
    return ConversationHandler.END


# ============ СПИСОК ИСТОЧНИКОВ ============

async def my_sources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    project = await require_project(update, context)
    
    if not project:
        return
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(SourceChannel).where(SourceChannel.project_id == project.id).order_by(SourceChannel.added_at.desc())
        )
        sources = result.scalars().all()
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one()
    
    if not sources:
        text = f"📭 В проекте «{project.name}» нет источников.\nДобавьте: /add_source"
        keyboard = [[InlineKeyboardButton("◀️ Назад к проекту", callback_data=f"project_menu_{project.id}")]]
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return
    
    text = f"📥 <b>Источники «{project.name}»</b> ({len(sources)} / {user.max_sources_per_project})\n\n"
    keyboard = []
    
    for src in sources:
        if src.source_type == "telegram":
            icon = "📡"
            name = f"@{src.channel_username}"
            
            criteria_parts = []
            if src.criteria:
                if "min_views" in src.criteria:
                    criteria_parts.append(f"👁 ≥{src.criteria['min_views']}")
                if "min_reactions" in src.criteria:
                    criteria_parts.append(f"❤️ ≥{src.criteria['min_reactions']}")
            criteria_str = ", ".join(criteria_parts) if criteria_parts else "без критериев"
            
            text += f"{icon} <b>{name}</b>\n"
            text += f"   📊 {criteria_str}\n"
            if src.include_keywords:
                text += f"   🔍 {src.include_keywords}\n"
        else:
            icon = "🎬"
            name = src.youtube_query or "популярное"
            
            text += f"{icon} <b>YouTube: {name}</b>\n"
            if src.youtube_category:
                text += f"   📁 Категория: {src.youtube_category}\n"
            if src.youtube_region:
                text += f"   🌍 Регион: {src.youtube_region}\n"
            text += f"   👁 ≥{src.min_views or 0} | 👍 ≥{src.min_likes or 0} | 💬 ≥{src.min_comments or 0}\n"
            if src.include_keywords:
                text += f"   🔍 {src.include_keywords}\n"
        
        status_icon = "✅" if src.is_active else "❌"
        text += f"   {status_icon} Активен\n"
        if src.last_parsed:
            text += f"   🕐 {src.last_parsed.strftime('%d.%m.%Y %H:%M')}\n"
        text += "\n"
        
        keyboard.append([
            InlineKeyboardButton(f"✏️ Ред. {name[:20]}", callback_data=f"edit_source_{src.id}"),
            InlineKeyboardButton(f"❌ Удалить", callback_data=f"del_source_{src.id}")
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад к проекту", callback_data=f"project_menu_{project.id}")])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )


# ============ УДАЛЕНИЕ ИСТОЧНИКА С ПОДТВЕРЖДЕНИЕМ ============

async def delete_source_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    source_id = int(query.data.replace("del_source_", ""))
    context.user_data['delete_source_id'] = source_id
    
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(SourceChannel).where(SourceChannel.id == source_id))
        source = result.scalar_one_or_none()
        if source.source_type == "telegram":
            source_name = f"@{source.channel_username}"
        else:
            source_name = f"YouTube: {source.youtube_query or 'популярное'}"
    
    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data="confirm_delete_source"),
         InlineKeyboardButton("❌ Отмена", callback_data="cancel_delete_source")]
    ]
    
    await query.message.reply_text(
        f"⚠️ Удалить источник {source_name}?\n\nПосты из этого источника больше не будут парситься.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    await query.delete_message()


async def confirm_delete_source_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    source_id = context.user_data.get('delete_source_id')
    if not source_id:
        await query.edit_message_text("❌ Ошибка: источник не найден")
        return
    
    async with AsyncSessionLocal() as session:
        await session.execute(delete(SourceChannel).where(SourceChannel.id == source_id))
        await session.commit()
    
    context.user_data.pop('delete_source_id', None)
    
    await query.edit_message_text("✅ Источник удалён")
    
    await my_sources(update, context)


async def cancel_delete_source_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    context.user_data.pop('delete_source_id', None)
    
    await query.edit_message_text("❌ Удаление отменено")
    
    await my_sources(update, context)


# ============ ВОЗВРАТ К ИСТОЧНИКАМ ============

async def back_to_sources_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await my_sources(update, context)