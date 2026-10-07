"""Finite-state definitions for the small admin edit flow."""

from aiogram.fsm.state import State, StatesGroup


class EditPost(StatesGroup):
    waiting_for_text = State()


class AddSource(StatesGroup):
    waiting_for_identifier = State()
    choosing_category = State()


class AddCategory(StatesGroup):
    waiting_for_name = State()


class RenameCategory(StatesGroup):
    waiting_for_name = State()


class SetCategoryDestination(StatesGroup):
    waiting_for_identifier = State()
