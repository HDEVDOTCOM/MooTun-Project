from datetime import date, datetime, timezone
from decimal import Decimal
import unittest

from parser import (
    CommandKind,
    SavingsGoalCommand,
    SavingsProgressCommand,
    SimpleCommand,
    TransactionCommand,
    UnresolvedCommand,
    parse_command,
)


TODAY = date(2026, 9, 14)


class TransactionParsingTests(unittest.TestCase):
    def test_simple_expense(self) -> None:
        command = parse_command("จ่าย 50 อาหาร", now=TODAY)

        self.assertEqual(
            command,
            TransactionCommand(CommandKind.EXPENSE, Decimal("50"), "อาหาร", TODAY),
        )

    def test_yesterday_expense_with_verb_attached_to_category(self) -> None:
        command = parse_command("เมื่อวานซื้อหนังสือ 320", now=TODAY)

        self.assertEqual(
            command,
            TransactionCommand(
                CommandKind.EXPENSE,
                Decimal("320"),
                "หนังสือ",
                date(2026, 9, 13),
            ),
        )

    def test_income(self) -> None:
        command = parse_command("รับ 500 ค่าขนม", now=TODAY)

        self.assertEqual(
            command,
            TransactionCommand(CommandKind.INCOME, Decimal("500"), "ค่าขนม", TODAY),
        )

    def test_command_words_inside_categories_are_not_false_positives(self) -> None:
        expense = parse_command("จ่าย 50 สำหรับอาหาร", now=TODAY)
        income = parse_command("รับ 500 ค่าเสียเวลา", now=TODAY)

        self.assertIsInstance(expense, TransactionCommand)
        self.assertEqual(expense.kind, CommandKind.EXPENSE)
        self.assertEqual(expense.category, "สำหรับอาหาร")
        self.assertIsInstance(income, TransactionCommand)
        self.assertEqual(income.kind, CommandKind.INCOME)
        self.assertEqual(income.category, "ค่าเสียเวลา")

    def test_filler_word_is_not_used_as_category(self) -> None:
        command = parse_command("จ่ายไป 100", now=TODAY)

        self.assertIsInstance(command, TransactionCommand)
        self.assertEqual(command.category, "อื่นๆ")

    def test_database_text_limits_are_reported_to_user(self) -> None:
        long_category = parse_command(f"จ่าย 50 {'ก' * 101}", now=TODAY)
        long_goal = parse_command(f"ตั้งเป้า 500 {'ข' * 201}", now=TODAY)

        self.assertIsInstance(long_category, UnresolvedCommand)
        self.assertEqual(long_category.kind, CommandKind.AMBIGUOUS)
        self.assertIsInstance(long_goal, UnresolvedCommand)
        self.assertEqual(long_goal.kind, CommandKind.AMBIGUOUS)

    def test_explicit_iso_date(self) -> None:
        command = parse_command("จ่าย 1,250.50 ค่าเดินทาง 2026-09-01", now=TODAY)

        self.assertEqual(command.amount, Decimal("1250.50"))
        self.assertEqual(command.category, "ค่าเดินทาง")
        self.assertEqual(command.transaction_date, date(2026, 9, 1))

    def test_thai_buddhist_date(self) -> None:
        command = parse_command("จ่าย 80 อาหาร 13/09/2569", now=TODAY)

        self.assertEqual(command.transaction_date, date(2026, 9, 13))

    def test_aware_datetime_is_converted_to_bangkok(self) -> None:
        utc_time = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
        command = parse_command("จ่าย 20", now=utc_time)

        self.assertEqual(command.transaction_date, date(2026, 9, 14))
        self.assertEqual(command.category, "อื่นๆ")


class OtherCommandTests(unittest.TestCase):
    def test_simple_commands(self) -> None:
        examples = {
            "ช่วยเหลือ": CommandKind.HELP,
            "ล่าสุด": CommandKind.RECENT,
            "สรุปเดือนนี้": CommandKind.MONTHLY_SUMMARY,
            "ลบล่าสุด": CommandKind.DELETE_LATEST,
            "เป้าหมาย": CommandKind.SAVINGS_STATUS,
            "เป้าหมายการออม": CommandKind.SAVINGS_STATUS,
            "ดูเป้าหมาย": CommandKind.SAVINGS_STATUS,
        }
        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(parse_command(text, now=TODAY), SimpleCommand(expected))

    def test_savings_goal(self) -> None:
        command = parse_command("ตั้งเป้า 1500 ซื้อหนังสือ", now=TODAY)

        self.assertEqual(
            command,
            SavingsGoalCommand(
                CommandKind.SET_SAVINGS_GOAL,
                Decimal("1500"),
                "ซื้อหนังสือ",
            ),
        )

    def test_savings_progress(self) -> None:
        command = parse_command("ออม 100", now=TODAY)

        self.assertEqual(
            command,
            SavingsProgressCommand(CommandKind.ADD_SAVINGS, Decimal("100")),
        )


class ConservativeParsingTests(unittest.TestCase):
    def assert_unresolved(self, text: str, kind: CommandKind) -> UnresolvedCommand:
        command = parse_command(text, now=TODAY)
        self.assertIsInstance(command, UnresolvedCommand)
        self.assertEqual(command.kind, kind)
        return command

    def test_does_not_guess_type(self) -> None:
        self.assert_unresolved("500 อาหาร", CommandKind.AMBIGUOUS)

    def test_does_not_guess_amount(self) -> None:
        self.assert_unresolved("จ่ายค่าอาหาร", CommandKind.AMBIGUOUS)

    def test_rejects_two_amounts(self) -> None:
        self.assert_unresolved("จ่าย 50 อาหาร 20", CommandKind.AMBIGUOUS)

    def test_rejects_both_transaction_types(self) -> None:
        self.assert_unresolved("รับแล้วจ่าย 500", CommandKind.AMBIGUOUS)

    def test_unknown_text(self) -> None:
        self.assert_unresolved("สวัสดี", CommandKind.UNKNOWN)

    def test_invalid_date(self) -> None:
        self.assert_unresolved("จ่าย 50 อาหาร 31/02/2026", CommandKind.AMBIGUOUS)


if __name__ == "__main__":
    unittest.main()
