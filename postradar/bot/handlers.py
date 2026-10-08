"""Telegram handlers for the single-admin review workflow."""

import logging
import json
import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from postradar.bot.review import AdminWorkflow
from postradar.bot.states import EditPost
from postradar.db.models import SourcePost

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

    @router.message(Command("captures"), F.chat.type == ChatType.PRIVATE)
    async def captures(message: Message) -> None:
        if not authorized(message.from_user.id if message.from_user else None):
            return
        arguments = (getattr(message, "text", None) or "").split()
        if len(arguments) > 2 or (len(arguments) == 2 and (
                not arguments[1].isascii() or not arguments[1].isdigit() or len(arguments[1]) > 18)):
            await message.answer("Использование: /captures [ID последнего поста]")
            return
        after_id = int(arguments[1]) if len(arguments) == 2 else 0
        async with workflow.session_factory() as session:
            rows = list((await session.scalars(select(SourcePost).where(
                SourcePost.status.in_(["CAPTURE_PENDING", "CAPTURE_FAILED", "CAPTURE_MISSING"]),
                SourcePost.id > after_id,
            ).order_by(SourcePost.id).limit(20))).all())
        lines = [f"Пост {post.id} · источник {post.source_id} · сообщение {post.telegram_message_id}\n"
                 f"{post.status} · попыток {post.capture_attempts or 0} · {(post.capture_error or '—')[:60]}"
                 for post in rows]
        report = "Незавершённый захват (до 20):\n" + "\n".join(lines)
        if len(rows) == 20:
            report += f"\nСледующая страница: /captures {rows[-1].id}"
        await message.answer(report if lines else "Незавершённого захвата нет.")

    @router.message(Command("reviewdeliveries"), F.chat.type == ChatType.PRIVATE)
    async def review_deliveries(message: Message) -> None:
        if not authorized(message.from_user.id if message.from_user else None):
            return
        async with workflow.session_factory() as session:
            rows = list((await session.scalars(select(SourcePost).where(
                SourcePost.status.in_(["REVIEW_DELIVERY_PENDING", "REVIEW_DELIVERY_UNCERTAIN"]),
            ).order_by(SourcePost.id).limit(20))).all())
        lines = []
        for post in rows:
            try:
                ids = json.loads(post.admin_message_ids or "[]")
            except (TypeError, ValueError):
                ids = []
            ids = [value for value in ids if type(value) is int and value > 0][:20]
            lines.append(
                f"Пост {post.id} · {post.status} · сообщения администратора: "
                f"{', '.join(map(str, ids)) if ids else 'не подтверждены'}"
            )
        await message.answer("Предпросмотры требуют сверки:\n" + "\n".join(lines)
                             if lines else "Неоднозначных предпросмотров нет.")

    @router.message(Command("publications"), F.chat.type == ChatType.PRIVATE)
    async def publications(message: Message) -> None:
        if not authorized(message.from_user.id if message.from_user else None):
            return
        await workflow.publication.recover_expired()
        attempts = await workflow.publication.unresolved()
        if not attempts:
            await message.answer("Незавершённых отправок нет.")
            return
        for attempt in attempts:
            parts = ", ".join(
                f"{part.position + 1}: {part.state} · ID {part.telegram_message_ids or '—'}"
                for part in attempt.parts[:20]
            )
            if len(attempt.parts) > 20:
                parts += f"\nВсего частей: {len(attempt.parts)}. Показаны первые 20."
            # Never display candidate text or provider errors in reconciliation diagnostics.
            await message.answer(
                f"Публикация {attempt.source_post_id} · {attempt.state}\n"
                f"Канал: {attempt.destination_id}\nПопытка: {attempt.id}\n{parts}\n"
                "Проверьте все части в канале назначения. Автоматического повтора нет.\n"
                f"Если вся публикация уже размещена: /confirm_published {attempt.id}",
            )

    @router.message(Command("confirm_published"), F.chat.type == ChatType.PRIVATE)
    async def confirm_published(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if not authorized(user_id):
            return
        fields = (message.text or "").split()
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{32}", fields[1]):
            await message.answer("Проверьте весь пост в канале, затем: /confirm_published ID_попытки")
            return
        result = await workflow.confirm_publication(fields[1], user_id)
        await message.answer(
            "Публикация отмечена как опубликованная по вашему подтверждению. Повторной отправки не было."
            if result == "published" else "Подтверждение недоступно: попытка активна, уже закрыта или состояние изменилось.",
        )

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
            "protected": "Источник защищён Telegram. Публикация заблокирована.",
            "protection_unverified": "Не удалось проверить защиту источника. Публикация заблокирована.",
            "failed": "Отправка не началась или Telegram отклонил запрос. Проверьте статус публикации.",
            "PUBLISHING": "Отправка уже началась. Не повторяйте её; статус доступен через /publications.",
            "PUBLISH_UNCERTAIN": "Часть публикации могла быть отправлена. Повтор заблокирован. Проверьте канал и /publications.",
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
        "PUBLISHING": "отправляется",
        "PUBLISH_UNCERTAIN": "результат отправки требует проверки",
        "REVIEW_DELIVERY_PENDING": "предпросмотр отправляется; результат требует проверки",
        "REVIEW_DELIVERY_UNCERTAIN": "предпросмотр требует проверки",
        "CAPTURE_PENDING": "захват ожидает восстановления",
        "CAPTURE_FAILED": "захват требует проверки",
        "CAPTURE_MISSING": "исходное сообщение недоступно",
        "SKIPPED": "пропущена",
        "FAILED": "ошибка",
        "FILTERED": "отфильтрована",
        "PROTECTED": "защищена Telegram",
        "PROTECTION_UNVERIFIED": "защита не проверена",
    }.get(status, "неизвестен")


async def _remove_review_controls(message: Message) -> None:
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception as error:
        logger.debug("Could not remove completed review controls: %s", type(error).__name__)
