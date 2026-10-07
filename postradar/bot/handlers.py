"""Telegram handlers for the single-admin review workflow."""

import logging
import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from postradar.bot.review import AdminWorkflow
from postradar.bot.states import EditPost

logger = logging.getLogger(__name__)
_CALLBACK = re.compile(r"^(publish|edit|skip):([1-9][0-9]*)$")


def create_admin_router(workflow: AdminWorkflow, admin_user_id: int) -> Router:
    router = Router(name="postradar_admin")

    def authorized(user_id: int | None) -> bool:
        return user_id == admin_user_id

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not authorized(message.from_user.id if message.from_user else None):
            await message.answer("У бота нет доступа для этого пользователя.")
            return
        await message.answer("Панель управления PostRadar готова.")

    @router.message(Command("cancel"), EditPost.waiting_for_text, F.chat.type == ChatType.PRIVATE)
    async def cancel_edit(message: Message, state: FSMContext) -> None:
        if not authorized(message.from_user.id if message.from_user else None):
            return
        await state.clear()
        await message.answer("Изменение отменено.")

    @router.callback_query(F.data.regexp(_CALLBACK))
    async def review_action(callback: CallbackQuery, state: FSMContext) -> None:
        user_id = callback.from_user.id if callback.from_user else None
        if not authorized(user_id):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        match = _CALLBACK.fullmatch(callback.data or "")
        if match is None:
            await callback.answer("Неизвестное действие.", show_alert=True)
            return
        action, raw_post_id = match.groups()
        post_id = int(raw_post_id)

        if action == "edit":
            status, post, _source = await workflow.action_status(post_id)
            if post is None:
                await callback.answer("Публикация не найдена.", show_alert=True)
            elif status != "REVIEW":
                await callback.answer(f"Публикация: {_status_label(status)}. Изменение недоступно.", show_alert=True)
            else:
                await state.update_data(post_id=post_id)
                await state.set_state(EditPost.waiting_for_text)
                await callback.answer()
                if callback.message:
                    await callback.message.answer("Отправьте новый текст или используйте /cancel.")
            return

        if action == "skip":
            result = await workflow.skip(post_id)
            message = {
                "skipped": "Публикация пропущена.",
                "missing": "Публикация не найдена.",
                "PUBLISHED": "Публикация уже опубликована.",
            }.get(result, f"Статус публикации: {_status_label(result)}.")
            await callback.answer(message, show_alert=result not in {"skipped"})
            if result == "skipped" and callback.message:
                await _remove_review_controls(callback.message)
            return

        result = await workflow.publish(post_id)
        messages = {
            "published": "Публикация отправлена в канал.",
            "PUBLISHED": "Публикация уже опубликована.",
            "SKIPPED": "Пропущенную публикацию нельзя отправить.",
            "missing": "Публикация не найдена.",
            "missing_category": "Категория публикации не найдена. Выберите категорию заново.",
            "category_disabled": "Категория отключена. Включите её или выберите другую.",
            "missing_destination": "Для этой публикации не настроен канал назначения.",
            "permission_error": "Не удалось опубликовать. Проверьте права бота в канале назначения.",
            "failed": "Не удалось опубликовать. Публикация осталась на проверке.",
        }
        await callback.answer(messages.get(result, f"Статус публикации: {_status_label(result)}."), show_alert=result != "published")
        if result == "published" and callback.message:
            await _remove_review_controls(callback.message)

    @router.message(EditPost.waiting_for_text, F.chat.type == ChatType.PRIVATE, F.text)
    async def receive_replacement(message: Message, state: FSMContext) -> None:
        if not authorized(message.from_user.id if message.from_user else None):
            return
        data = await state.get_data()
        post_id = data.get("post_id")
        if not isinstance(post_id, int):
            await state.clear()
            await message.answer("Сессия изменения устарела. Нажмите «Изменить» под публикацией ещё раз.")
            return
        result = await workflow.save_edit(post_id, message.text or "")
        if result == "empty":
            await message.answer("Текст не должен быть пустым. Отправьте текст или используйте /cancel.")
            return
        await state.clear()
        responses = {
            "edited": "Изменение сохранено. Используйте кнопки публикации для дальнейших действий.",
            "missing": "Публикация не найдена.",
            "preview_failed": "Изменение сохранено, но предпросмотр не обновился. Публикация осталась на проверке.",
        }
        await message.answer(responses.get(result, f"Статус публикации: {_status_label(result)}. Изменение недоступно."))

    return router


def _status_label(status: str) -> str:
    return {
        "NEW": "новая",
        "REVIEW": "на проверке",
        "PUBLISHED": "опубликована",
        "SKIPPED": "пропущена",
        "FAILED": "ошибка",
    }.get(status, "неизвестен")


async def _remove_review_controls(message: Message) -> None:
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception as error:
        logger.debug("Could not remove completed review controls: %s", type(error).__name__)
