"""Admin-only source, Category, destination, and review-routing controls."""

import logging
import re
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from postradar.bot.keyboards import (
    category_choice_keyboard,
    category_detail_keyboard,
    category_list_keyboard,
    main_menu_keyboard,
    source_detail_keyboard,
    source_list_keyboard,
    source_menu_keyboard,
)
from postradar.bot.review import AdminWorkflow
from postradar.bot.states import AddCategory, AddSource, RenameCategory, SetCategoryDestination
from postradar.services.management import (
    DestinationSetupError,
    ManagementError,
    ManagementService,
)
from postradar.telegram.source_client import SourceResolutionError

logger = logging.getLogger(__name__)
_CALLBACK = re.compile(
    r"^(?:menu:(?:home|sources|categories|pending|status)|sources:(?:add|list)|"
    r"cats:(?:add|list)|src:(?:view|toggle|remove|category):\d+|"
    r"src:setcat:\d+:\d+|source:addcat:\d+|"
    r"cat:(?:view|rename|destination|toggle|delete):\d+|route:\d+|"
    r"route:set:\d+:\d+|route:back:\d+)$"
)


def create_management_router(
    service: ManagementService,
    workflow: AdminWorkflow,
    admin_user_id: int,
) -> Router:
    router = Router(name="postradar_management")

    def is_admin(message_or_callback: Message | CallbackQuery) -> bool:
        user = message_or_callback.from_user
        return user is not None and user.id == admin_user_id

    @router.message(Command("menu"), F.chat.type == ChatType.PRIVATE)
    async def menu_command(message: Message) -> None:
        if not is_admin(message):
            await message.answer("У бота нет доступа для этого пользователя.")
            return
        await message.answer("Меню PostRadar", reply_markup=main_menu_keyboard())

    @router.message(Command("sources"), F.chat.type == ChatType.PRIVATE)
    async def sources_command(message: Message) -> None:
        if not is_admin(message):
            await message.answer("У бота нет доступа для этого пользователя.")
            return
        await render_sources(message)

    @router.message(Command("categories"), F.chat.type == ChatType.PRIVATE)
    async def categories_command(message: Message) -> None:
        if not is_admin(message):
            await message.answer("У бота нет доступа для этого пользователя.")
            return
        await render_categories(message)

    @router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
    async def cancel_flow(message: Message, state: FSMContext) -> None:
        if not is_admin(message):
            return
        current = await state.get_state()
        if current is None:
            await message.answer("Нет активного действия для отмены.")
            return
        await state.clear()
        await message.answer("Действие отменено.")

    @router.callback_query(F.data.regexp(_CALLBACK))
    async def management_action(callback: CallbackQuery, state: FSMContext) -> None:
        if not is_admin(callback):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        data = callback.data or ""
        parts = data.split(":")
        try:
            if data == "menu:home":
                await callback.answer()
                await render_callback(callback, "Меню PostRadar", main_menu_keyboard())
            elif data == "menu:sources":
                await callback.answer()
                await render_callback(callback, "Источники", source_menu_keyboard())
            elif data in {"menu:categories", "cats:list"}:
                await callback.answer()
                await render_category_list(callback)
            elif data == "sources:list":
                await callback.answer()
                await render_source_list(callback)
            elif data == "menu:pending":
                new_count, review_count = await service.pending_counts()
                await callback.answer()
                await render_callback(
                    callback,
                    f"На проверке\nНовые: {new_count}\nОжидают проверки: {review_count}",
                    main_menu_keyboard(),
                )
            elif data == "menu:status":
                sources = await service.list_sources()
                categories = await service.list_categories()
                new_count, review_count = await service.pending_counts()
                enabled_sources = sum(1 for source in sources if source.enabled)
                enabled_categories = sum(1 for item in categories if item.category.enabled)
                await callback.answer()
                await render_callback(
                    callback,
                    "Статус\n"
                    f"Включено источников: {enabled_sources}\n"
                    f"Включено категорий: {enabled_categories}\n"
                    f"Новые: {new_count} · На проверке: {review_count}",
                    main_menu_keyboard(),
                )
            elif data == "sources:add":
                await state.clear()
                await state.set_state(AddSource.waiting_for_identifier)
                await callback.answer()
                await reply_callback(callback, "Отправьте @username, ссылку t.me или числовой ID канала. Для отмены используйте /cancel.")
            elif data == "cats:add":
                await state.clear()
                await state.set_state(AddCategory.waiting_for_name)
                await callback.answer()
                await reply_callback(callback, "Отправьте название новой категории. Для отмены используйте /cancel.")
            elif parts[0] == "src" and parts[1] == "view":
                await callback.answer()
                await render_source_detail(callback, int(parts[2]))
            elif parts[0] == "src" and parts[1] == "toggle":
                source_id = int(parts[2])
                source = await service.get_source(source_id)
                if source is None:
                    raise ManagementError("Источник не найден.")
                await service.set_source_enabled(source_id, not source.enabled)
                await callback.answer("Статус источника изменён.")
                await render_source_detail(callback, source_id)
            elif parts[0] == "src" and parts[1] == "remove":
                source = await service.remove_source(int(parts[2]))
                await callback.answer("Источник отключён, история публикаций сохранена.")
                await render_source_detail(callback, source.id)
            elif parts[0] == "src" and parts[1] == "category":
                source = await service.get_source(int(parts[2]))
                if source is None:
                    raise ManagementError("Источник не найден.")
                categories = await service.list_categories(enabled_only=True)
                if not categories:
                    raise ManagementError("Сначала создайте и включите категорию.")
                await callback.answer()
                await render_callback(
                    callback,
                    f"Выберите категорию по умолчанию для {source.title or source.telegram_chat_id}.\n"
                    f"Сейчас: {source.category.name if source.category else 'не назначена'}",
                    category_choice_keyboard(
                        categories,
                        callback_prefix="src:setcat",
                        first_id=source.id,
                        current_category_id=source.category_id,
                    ),
                )
            elif parts[0] == "src" and parts[1] == "setcat":
                source_id, category_id = int(parts[2]), int(parts[3])
                await service.set_source_category(source_id, category_id)
                await callback.answer("Категория источника изменена.")
                await render_source_detail(callback, source_id)
            elif parts[0] == "source" and parts[1] == "addcat":
                if await state.get_state() != AddSource.choosing_category.state:
                    raise ManagementError("Добавление источника устарело. Начните заново.")
                data = await state.get_data()
                resolved_data = data.get("resolved_source")
                if not isinstance(resolved_data, dict):
                    raise ManagementError("Добавление источника устарело. Начните заново.")
                source = await service.create_source(
                    _resolved_source_from_state(resolved_data), int(parts[2])
                )
                await state.clear()
                await callback.answer("Источник добавлен.")
                await render_source_detail(callback, source.id)
            elif parts[0] == "cat" and parts[1] == "view":
                await callback.answer()
                await render_category_detail(callback, int(parts[2]))
            elif parts[0] == "cat" and parts[1] == "rename":
                await state.clear()
                await state.update_data(category_id=int(parts[2]))
                await state.set_state(RenameCategory.waiting_for_name)
                await callback.answer()
                await reply_callback(callback, "Отправьте новое название категории или используйте /cancel.")
            elif parts[0] == "cat" and parts[1] == "destination":
                await state.clear()
                await state.update_data(category_id=int(parts[2]))
                await state.set_state(SetCategoryDestination.waiting_for_identifier)
                await callback.answer()
                await reply_callback(callback, "Отправьте @username или числовой ID канала назначения либо используйте /cancel.")
            elif parts[0] == "cat" and parts[1] == "toggle":
                category = await service.get_category(int(parts[2]))
                if category is None:
                    raise ManagementError("Категория не найдена.")
                await service.set_category_enabled(category.id, not category.enabled)
                await callback.answer("Статус категории изменён.")
                await render_category_detail(callback, category.id)
            elif parts[0] == "cat" and parts[1] == "delete":
                await service.delete_category(int(parts[2]))
                await callback.answer("Категория удалена.")
                await render_category_list(callback)
            elif parts[0] == "route" and parts[1] == "set":
                post_id, category_id = int(parts[2]), int(parts[3])
                category_name = await workflow.set_post_category(post_id, category_id)
                await workflow.refresh_review_controls(post_id)
                await callback.answer(f"Категория изменена: {category_name}.")
                if callback.message is not None:
                    await callback.message.edit_text(
                        f"Категория изменена: {category_name}. Используйте кнопки под исходным сообщением."
                    )
            elif parts[0] == "route" and parts[1] == "back":
                await workflow.refresh_review_controls(int(parts[2]))
                await callback.answer()
                if callback.message is not None:
                    await callback.message.edit_text(
                        "Выбор категории закрыт. Кнопки проверки находятся под исходным сообщением."
                    )
            elif parts[0] == "route":
                post_id = int(parts[1])
                status, post, _source = await workflow.action_status(post_id)
                if post is None:
                    raise ManagementError("Публикация не найдена.")
                if status != "REVIEW":
                    raise ManagementError("Категорию можно изменить только у публикации на проверке.")
                categories = await service.list_categories(enabled_only=True)
                if not categories:
                    raise ManagementError("Нет включённых категорий.")
                current_name = post.category.name if post.category else "не задана"
                await callback.answer()
                await reply_callback(
                    callback,
                    f"Текущая категория: {current_name}\nВыберите категорию для этой публикации:",
                    category_choice_keyboard(
                        categories,
                        callback_prefix="route:set",
                        first_id=post_id,
                        current_category_id=post.category_id,
                        back_callback=f"route:back:{post_id}",
                    ),
                )
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
        except Exception as error:
            logger.error(
                "Admin management action failed: action=%s exception=%s",
                data.split(":", 1)[0],
                type(error).__name__,
            )
            await callback.answer(
                "Не удалось выполнить действие. Попробуйте ещё раз.", show_alert=True
            )

    @router.message(AddSource.waiting_for_identifier, F.chat.type == ChatType.PRIVATE, F.text)
    async def add_source_identifier(message: Message, state: FSMContext) -> None:
        if not is_admin(message):
            return
        try:
            resolved = await service.resolve_source(message.text or "")
            categories = await service.list_categories(enabled_only=True)
            if not categories:
                await state.clear()
                await message.answer("Сначала создайте и включите категорию командой /categories.")
                return
            await state.update_data(
                resolved_source={
                    "telegram_chat_id": resolved.telegram_chat_id,
                    "title": resolved.title,
                    "username": resolved.username,
                }
            )
            await state.set_state(AddSource.choosing_category)
            await message.answer(
                f"Источник: {resolved.title}\nВыберите категорию по умолчанию:",
                reply_markup=category_choice_keyboard(categories, callback_prefix="source:addcat"),
            )
        except ManagementError as error:
            await message.answer(str(error))
        except SourceResolutionError as error:
            await message.answer(_source_resolution_error_ru(error))

    @router.message(AddCategory.waiting_for_name, F.chat.type == ChatType.PRIVATE, F.text)
    async def add_category_name(message: Message, state: FSMContext) -> None:
        if not is_admin(message):
            return
        try:
            category = await service.create_category(message.text or "")
        except ManagementError as error:
            await message.answer(str(error))
            return
        await state.clear()
        await message.answer(
            f"Категория создана: {category.name}. Канал назначения можно настроить позже.",
            reply_markup=category_detail_keyboard(category),
        )

    @router.message(RenameCategory.waiting_for_name, F.chat.type == ChatType.PRIVATE, F.text)
    async def rename_category_name(message: Message, state: FSMContext) -> None:
        if not is_admin(message):
            return
        category_id = (await state.get_data()).get("category_id")
        try:
            if not isinstance(category_id, int):
                raise ManagementError("Действие переименования устарело. Откройте категорию заново.")
            category = await service.rename_category(category_id, message.text or "")
        except ManagementError as error:
            await message.answer(str(error))
            return
        await state.clear()
        await message.answer(
            f"Категория переименована: {category.name}.", reply_markup=category_detail_keyboard(category)
        )

    @router.message(SetCategoryDestination.waiting_for_identifier, F.chat.type == ChatType.PRIVATE, F.text)
    async def set_category_destination(message: Message, state: FSMContext) -> None:
        if not is_admin(message):
            return
        category_id = (await state.get_data()).get("category_id")
        try:
            if not isinstance(category_id, int):
                raise ManagementError("Настройка канала устарела. Откройте категорию заново.")
            category = await service.set_destination(category_id, message.text or "")
        except DestinationSetupError as error:
            await message.answer(str(error))
            return
        except ManagementError as error:
            await message.answer(str(error))
            return
        await state.clear()
        await message.answer(
            f"Канал назначения установлен: {category.destination_title} ({category.destination_channel_id}).",
            reply_markup=category_detail_keyboard(category),
        )

    async def render_sources(message: Message) -> None:
        await message.answer("Управление источниками Telegram", reply_markup=source_menu_keyboard())

    async def render_categories(message: Message) -> None:
        categories = await service.list_categories()
        await message.answer(_category_list_text(categories), reply_markup=category_list_keyboard(categories))

    async def render_source_list(callback: CallbackQuery) -> None:
        sources = await service.list_sources()
        await render_callback(callback, _source_list_text(sources), source_list_keyboard(sources))

    async def render_category_list(callback: CallbackQuery) -> None:
        categories = await service.list_categories()
        await render_callback(callback, _category_list_text(categories), category_list_keyboard(categories))

    async def render_source_detail(callback: CallbackQuery, source_id: int) -> None:
        source = await service.get_source(source_id)
        if source is None:
            raise ManagementError("Источник не найден.")
        category = source.category.name if source.category else "не назначена"
        title = source.title or "Без названия"
        username = f"@{source.username}" if source.username else "не указан"
        text = f"{title}\nИмя пользователя: {username}"
        text += (
            f"\nID Telegram: {source.telegram_chat_id}\n"
            f"Категория: {category}\n"
            f"Статус: {'включён' if source.enabled else 'выключен'}"
        )
        await render_callback(callback, text, source_detail_keyboard(source))

    async def render_category_detail(callback: CallbackQuery, category_id: int) -> None:
        category = await service.get_category(category_id)
        if category is None:
            raise ManagementError("Категория не найдена.")
        summary = next(
            (item for item in await service.list_categories() if item.category.id == category_id),
            None,
        )
        destination = category.destination_title or "не настроен"
        if category.destination_title is None and category.destination_channel_id is not None:
            destination = str(category.destination_channel_id)
        text = (
            f"Категория: {category.name}\nКанал назначения: {destination}\n"
            f"Источников: {summary.source_count if summary else 0}\n"
            f"Статус: {'включена' if category.enabled else 'выключена'}"
        )
        await render_callback(callback, text, category_detail_keyboard(category))

    return router


async def render_callback(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup)


async def reply_callback(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if callback.message is not None:
        await callback.message.answer(text, reply_markup=reply_markup)


def _source_list_text(sources: list) -> str:
    if not sources:
        return "Источников пока нет. Сначала создайте и включите категорию."
    lines = ["Источники:"]
    for source in sources[:20]:
        title = _short_display(source.title or "Без названия", 64)
        username = f"@{source.username}" if source.username else "имя пользователя не указано"
        category = _short_display(source.category.name, 40) if source.category else "не назначена"
        state = "включён" if source.enabled else "выключен"
        lines.append(
            f"{'●' if source.enabled else '○'} {title}\n"
            f"  {username}\n"
            f"  Категория: {category} · {state}"
        )
    if len(sources) > 20:
        lines.append("Откройте источник ниже, чтобы посмотреть подробности.")
    return "\n".join(lines)


def _category_list_text(categories: list) -> str:
    if not categories:
        return "Категорий пока нет. Добавьте категорию для источников и каналов назначения."
    lines = ["Категории:"]
    for item in categories[:20]:
        category = item.category
        destination = _short_display(category.destination_title, 80) if category.destination_title else (
            str(category.destination_channel_id)
            if category.destination_channel_id is not None
            else "канал не задан"
        )
        state = "включена" if category.enabled else "выключена"
        lines.append(
            f"{'●' if category.enabled else '○'} {_short_display(category.name, 64)}\n"
            f"  Канал: {destination} · Источников: {item.source_count} · {state}"
        )
    if len(categories) > 20:
        lines.append("Откройте категорию ниже, чтобы посмотреть подробности.")
    return "\n".join(lines)


def _resolved_source_from_state(data: dict) -> Any:
    from postradar.telegram.source_client import ResolvedSource

    try:
        return ResolvedSource(
            telegram_chat_id=int(data["telegram_chat_id"]),
            title=str(data["title"]),
            username=str(data["username"]) if data.get("username") else None,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ManagementError("Добавление источника не завершено. Начните заново.") from error


def _short_display(value: str, max_length: int) -> str:
    """Shorten presentation text while retaining identifiers such as usernames."""
    return value if len(value) <= max_length else value[: max_length - 1] + "…"


def _source_resolution_error_ru(error: SourceResolutionError) -> str:
    """Translate safe source resolver errors for the Russian operator UI."""
    detail = str(error).lower()
    if "cannot access" in detail:
        return "Аккаунт Telethon не видит этот канал. Вступите в него в Telegram и попробуйте снова."
    if "not a channel or supergroup" in detail:
        return "Выбранный чат не является каналом или супергруппой."
    if "invite links" in detail:
        return "Ссылки-приглашения не поддерживаются. Укажите username или числовой ID канала."
    if "invalid" in detail or "malformed" in detail:
        return "Не удалось распознать ссылку или ID. Проверьте введённые данные."
    return "Не удалось определить канал. Проверьте username, ссылку или числовой ID."
