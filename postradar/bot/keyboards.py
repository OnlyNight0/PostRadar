"""Inline controls for source post review."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from postradar.db.models import Category, Source
from postradar.services.management import CategorySummary


def review_keyboard(post_id: int, category_name: str | None = None) -> InlineKeyboardMarkup:
    """Build review actions using only the persisted post identifier."""
    category_label = category_name or "не задана"
    if len(category_label) > 48:
        category_label = category_label[:45] + "..."
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Опубликовать", callback_data=f"publish:{post_id}"),
                InlineKeyboardButton(text="Изменить", callback_data=f"edit:{post_id}"),
                InlineKeyboardButton(text="Пропустить", callback_data=f"skip:{post_id}"),
            ],
            [
                InlineKeyboardButton(
                    text=f"Категория: {category_label}", callback_data=f"route:{post_id}"
                )
            ],
        ]
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Источники", callback_data="menu:sources")],
            [InlineKeyboardButton(text="Категории", callback_data="menu:categories")],
            [InlineKeyboardButton(text="На проверке", callback_data="menu:pending")],
            [InlineKeyboardButton(text="Статус", callback_data="menu:status")],
        ]
    )


def source_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить источник", callback_data="sources:add")],
            [InlineKeyboardButton(text="Список источников", callback_data="sources:list")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:home")],
        ]
    )


def source_list_keyboard(sources: list[Source]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'●' if source.enabled else '○'} {(source.title or source.username or str(source.telegram_chat_id))[:48]}",
                callback_data=f"src:view:{source.id}",
            )
        ]
        for source in sources[:30]
    ]
    rows.extend(
        [
            [InlineKeyboardButton(text="Добавить источник", callback_data="sources:add")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:sources")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def source_detail_keyboard(source: Source) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить категорию", callback_data=f"src:category:{source.id}")],
            [InlineKeyboardButton(
                text="Выключить" if source.enabled else "Включить",
                callback_data=f"src:toggle:{source.id}",
            )],
            [InlineKeyboardButton(text="Удалить (отключить)", callback_data=f"src:remove:{source.id}")],
            [InlineKeyboardButton(text="Назад", callback_data="sources:list")],
        ]
    )


def category_list_keyboard(categories: list[CategorySummary]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'●' if item.category.enabled else '○'} {item.category.name[:38]} ({item.source_count})",
                callback_data=f"cat:view:{item.category.id}",
            )
        ]
        for item in categories[:30]
    ]
    rows.extend(
        [
            [InlineKeyboardButton(text="Добавить категорию", callback_data="cats:add")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:home")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_detail_keyboard(category: Category) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Переименовать", callback_data=f"cat:rename:{category.id}")],
            [InlineKeyboardButton(text="Канал назначения", callback_data=f"cat:destination:{category.id}")],
            [InlineKeyboardButton(
                text="Выключить" if category.enabled else "Включить",
                callback_data=f"cat:toggle:{category.id}",
            )],
            [InlineKeyboardButton(text="Удалить", callback_data=f"cat:delete:{category.id}")],
            [InlineKeyboardButton(text="Назад", callback_data="cats:list")],
        ]
    )


def category_choice_keyboard(
    categories: list[CategorySummary],
    *,
    callback_prefix: str,
    first_id: int | None = None,
    current_category_id: int | None = None,
    back_callback: str = "menu:home",
) -> InlineKeyboardMarkup:
    rows = []
    for item in categories[:30]:
        category = item.category
        marker = "✓ " if category.id == current_category_id else ""
        callback_data = (
            f"{callback_prefix}:{first_id}:{category.id}"
            if first_id is not None
            else f"{callback_prefix}:{category.id}"
        )
        rows.append(
            [InlineKeyboardButton(text=f"{marker}{category.name[:48]}", callback_data=callback_data)]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
