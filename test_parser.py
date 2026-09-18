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
    def assert_transaction(
        self,
        text: str,
        *,
        kind: CommandKind,
        amount: str,
        category: str,
        description: str,
        transaction_date: date = TODAY,
    ) -> TransactionCommand:
        command = parse_command(text, now=TODAY)
        self.assertIsInstance(command, TransactionCommand)
        self.assertEqual(command.kind, kind)
        self.assertEqual(command.amount, Decimal(amount))
        self.assertEqual(command.category, category)
        self.assertEqual(command.description, description)
        self.assertEqual(command.transaction_date, transaction_date)
        self.assertTrue(command.inference_rule)
        return command

    def test_explicit_expense_still_works(self) -> None:
        command = self.assert_transaction(
            "จ่าย 50 อาหาร",
            kind=CommandKind.EXPENSE,
            amount="50",
            category="อาหาร",
            description="อาหาร",
        )
        self.assertEqual(command.inference_rule, "explicit.จ่าย")

    def test_yesterday_expense_with_verb_attached_to_item(self) -> None:
        self.assert_transaction(
            "เมื่อวานซื้อหนังสือ 320",
            kind=CommandKind.EXPENSE,
            amount="320",
            category="การศึกษา",
            description="หนังสือ",
            transaction_date=date(2026, 9, 13),
        )

    def test_explicit_income_still_works(self) -> None:
        self.assert_transaction(
            "รับ 500 ค่าขนม",
            kind=CommandKind.INCOME,
            amount="500",
            category="รายรับอื่นๆ",
            description="ค่าขนม",
        )

    def test_attached_explicit_aliases_remain_supported(self) -> None:
        examples = (
            ("รับค่าขนม 500", CommandKind.INCOME, "500", "รายรับอื่นๆ", "ค่าขนม", "รับ"),
            ("รับข้าว 50", CommandKind.INCOME, "50", "รายรับอื่นๆ", "ข้าว", "รับ"),
            ("เสียค่าซ่อม 500", CommandKind.EXPENSE, "500", "อื่นๆ", "ค่าซ่อม", "เสีย"),
            ("เสียโบนัส 500", CommandKind.EXPENSE, "500", "อื่นๆ", "โบนัส", "เสีย"),
        )
        for text, kind, amount, category, description, rule_word in examples:
            with self.subTest(text=text):
                command = self.assert_transaction(
                    text,
                    kind=kind,
                    amount=amount,
                    category=category,
                    description=description,
                )
                self.assertEqual(command.inference_rule, f"explicit.{rule_word}")

    def test_received_alias_requires_a_supported_boundary(self) -> None:
        for text in ("ได้รับบท 50", "ได้รับประทานข้าว 50"):
            with self.subTest(text=text):
                command = parse_command(text, now=TODAY)
                self.assertIsInstance(command, UnresolvedCommand)
                self.assertEqual(
                    command.reason,
                    "กรุณาระบุว่าเป็นรายรับหรือรายจ่าย",
                )

        for text in ("ได้รับโบนัส 500", "ได้รับ 500 โบนัส"):
            with self.subTest(text=text):
                command = self.assert_transaction(
                    text,
                    kind=CommandKind.INCOME,
                    amount="500",
                    category="เงินได้รับ",
                    description="โบนัส",
                )
                self.assertEqual(command.inference_rule, "explicit.ได้รับ")

    def test_clear_explicit_aliases_accept_unknown_attached_items(self) -> None:
        examples = (
            ("ซื้อโทรศัพท์ 30000", CommandKind.EXPENSE, "30000", "อื่นๆ", "โทรศัพท์"),
            ("รายจ่ายซ่อมรถ 500", CommandKind.EXPENSE, "500", "อื่นๆ", "ซ่อมรถ"),
            ("เงินเข้าบัญชี 500", CommandKind.INCOME, "500", "รายรับอื่นๆ", "บัญชี"),
        )
        for text, kind, amount, category, description in examples:
            with self.subTest(text=text):
                self.assert_transaction(
                    text,
                    kind=kind,
                    amount=amount,
                    category=category,
                    description=description,
                )

    def test_amount_first_explicit_aliases_do_not_reverse_direction(self) -> None:
        examples = (
            ("50 รับข้าว", CommandKind.INCOME, "50", "รายรับอื่นๆ", "ข้าว"),
            ("50 รับ ข้าว", CommandKind.INCOME, "50", "รายรับอื่นๆ", "ข้าว"),
            ("50 บาท รับข้าว", CommandKind.INCOME, "50", "รายรับอื่นๆ", "ข้าว"),
            ("500 เสียโบนัส", CommandKind.EXPENSE, "500", "อื่นๆ", "โบนัส"),
            ("500 เสีย โบนัส", CommandKind.EXPENSE, "500", "อื่นๆ", "โบนัส"),
            ("500 บาท เสียโบนัส", CommandKind.EXPENSE, "500", "อื่นๆ", "โบนัส"),
        )
        for text, kind, amount, category, description in examples:
            with self.subTest(text=text):
                self.assert_transaction(
                    text,
                    kind=kind,
                    amount=amount,
                    category=category,
                    description=description,
                )

    def test_expense_word_and_amount_order_variants(self) -> None:
        for text in ("จ่ายข้าว 50", "จ่าย 50 ข้าว", "ซื้อข้าว 50", "500 อาหาร"):
            with self.subTest(text=text):
                self.assert_transaction(
                    text,
                    kind=CommandKind.EXPENSE,
                    amount="50" if text != "500 อาหาร" else "500",
                    category="อาหาร",
                    description="อาหาร" if text == "500 อาหาร" else "ข้าว",
                )

    def test_natural_expenses_are_categorized(self) -> None:
        examples = (
            ("ข้าว 50", "50", "อาหาร", "ข้าว"),
            ("ข้าวมันไก่ 50 บาท", "50", "อาหาร", "ข้าวมันไก่"),
            ("กาแฟ 65", "65", "อาหาร", "กาแฟ"),
            ("BTS 47", "47", "เดินทาง", "BTS"),
            ("เติมน้ำมัน 1,000", "1000", "เดินทาง", "เติมน้ำมัน"),
            ("ค่าหอ 5000", "5000", "ที่พัก", "ค่าหอ"),
            ("ค่าเน็ต 599", "599", "บิลและบริการ", "ค่าเน็ต"),
            ("ข้าว ๕๐", "50", "อาหาร", "ข้าว"),
        )
        for text, amount, category, description in examples:
            with self.subTest(text=text):
                self.assert_transaction(
                    text,
                    kind=CommandKind.EXPENSE,
                    amount=amount,
                    category=category,
                    description=description,
                )

    def test_natural_incomes_are_categorized(self) -> None:
        examples = (
            ("เงินเดือน 20000", "20000", "เงินเดือน", "เงินเดือน"),
            ("เงินเดือนเข้า 20,000", "20000", "เงินเดือน", "เงินเดือนเข้า"),
            ("ขายของ 350", "350", "ขายของ", "ขายของ"),
            ("แม่ให้ 500", "500", "เงินได้รับ", "แม่ให้"),
        )
        for text, amount, category, description in examples:
            with self.subTest(text=text):
                self.assert_transaction(
                    text,
                    kind=CommandKind.INCOME,
                    amount=amount,
                    category=category,
                    description=description,
                )

    def test_strong_income_action_overrides_expense_object(self) -> None:
        self.assert_transaction(
            "ขายข้าว 50",
            kind=CommandKind.INCOME,
            amount="50",
            category="ขายของ",
            description="ขายข้าว",
        )

    def test_explicit_type_overrides_incompatible_semantic_type(self) -> None:
        self.assert_transaction(
            "จ่ายเงินเดือน 20000",
            kind=CommandKind.EXPENSE,
            amount="20000",
            category="อื่นๆ",
            description="เงินเดือน",
        )
        self.assert_transaction(
            "รับ 50 ข้าว",
            kind=CommandKind.INCOME,
            amount="50",
            category="รายรับอื่นๆ",
            description="ข้าว",
        )

    def test_command_prefixes_inside_words_are_not_false_positives(self) -> None:
        self.assert_transaction(
            "รับประทานข้าว 50",
            kind=CommandKind.EXPENSE,
            amount="50",
            category="อาหาร",
            description="รับประทานข้าว",
        )
        unresolved = parse_command("เสียใจ 50", now=TODAY)
        self.assertIsInstance(unresolved, UnresolvedCommand)
        self.assertEqual(unresolved.reason, "กรุณาระบุว่าเป็นรายรับหรือรายจ่าย")

        for text in (
            "รับทราบ 50",
            "รับบท 50",
            "รับประกัน 500",
            "เสียหลัก 50",
            "รับชาติ 500",
            "รับชาวบ้าน 500",
            "เสียชาตินี้ 500",
        ):
            with self.subTest(text=text):
                unresolved = parse_command(text, now=TODAY)
                self.assertIsInstance(unresolved, UnresolvedCommand)

    def test_linked_text_does_not_treat_embedded_prefix_as_income(self) -> None:
        self.assert_transaction(
            "จ่ายแล้วรับประทานข้าว 50",
            kind=CommandKind.EXPENSE,
            amount="50",
            category="อาหาร",
            description="รับประทานข้าว",
        )

    def test_semantic_matches_respect_phrase_boundaries(self) -> None:
        expense_contexts = (
            ("ค่าเช่าร้านขายของ 5000", "5000", "ที่พัก"),
            ("ค่าเช่าห้องสอนพิเศษ 5000", "5000", "ที่พัก"),
            ("ค่าไฟห้องสอนพิเศษ 500", "500", "บิลและบริการ"),
            ("ค่าเน็ตสอนพิเศษ 599", "599", "บิลและบริการ"),
            ("ค่าหอสอนพิเศษ 5000", "5000", "ที่พัก"),
            ("ค่าเดินทางไปสอนพิเศษ 50", "50", "เดินทาง"),
            ("หนังสือสอนพิเศษ 500", "500", "การศึกษา"),
        )
        for text, amount, category in expense_contexts:
            with self.subTest(text=text):
                self.assert_transaction(
                    text,
                    kind=CommandKind.EXPENSE,
                    amount=amount,
                    category=category,
                    description=text.removesuffix(f" {amount}"),
                )
        self.assert_transaction(
            "สอนพิเศษ 500",
            kind=CommandKind.INCOME,
            amount="500",
            category="งานพิเศษ",
            description="สอนพิเศษ",
        )
        unresolved = parse_command("ค่าสอนพิเศษ 500", now=TODAY)
        self.assertIsInstance(unresolved, UnresolvedCommand)
        unresolved = parse_command("วิชา 50", now=TODAY)
        self.assertIsInstance(unresolved, UnresolvedCommand)
        self.assertEqual(unresolved.reason, "กรุณาระบุว่าเป็นรายรับหรือรายจ่าย")
        for text in ("ภาษีเงินเดือน 500", "ค่ายางรถ 500"):
            with self.subTest(text=text):
                unresolved = parse_command(text, now=TODAY)
                self.assertIsInstance(unresolved, UnresolvedCommand)
                self.assertEqual(
                    unresolved.reason,
                    "กรุณาระบุว่าเป็นรายรับหรือรายจ่าย",
                )

    def test_explicit_type_without_item_uses_typed_fallback(self) -> None:
        expense = self.assert_transaction(
            "จ่าย 50",
            kind=CommandKind.EXPENSE,
            amount="50",
            category="อื่นๆ",
            description="ไม่ระบุรายการ",
        )
        income = self.assert_transaction(
            "รับ 500",
            kind=CommandKind.INCOME,
            amount="500",
            category="รายรับอื่นๆ",
            description="ไม่ระบุรายการ",
        )
        self.assertEqual(expense.inference_rule, "explicit.จ่าย")
        self.assertEqual(income.inference_rule, "explicit.รับ")

    def test_filler_word_is_not_used_as_item(self) -> None:
        self.assert_transaction(
            "จ่ายไป 100",
            kind=CommandKind.EXPENSE,
            amount="100",
            category="อื่นๆ",
            description="ไม่ระบุรายการ",
        )

    def test_database_text_limits_are_reported_to_user(self) -> None:
        long_item = parse_command(f"จ่าย 50 {'ก' * 501}", now=TODAY)
        long_goal = parse_command(f"ตั้งเป้า 500 {'ข' * 201}", now=TODAY)
        self.assertIsInstance(long_item, UnresolvedCommand)
        self.assertEqual(long_item.kind, CommandKind.AMBIGUOUS)
        self.assertIsInstance(long_goal, UnresolvedCommand)
        self.assertEqual(long_goal.kind, CommandKind.AMBIGUOUS)

    def test_explicit_iso_date_can_appear_before_command(self) -> None:
        command = self.assert_transaction(
            "2026-09-01 จ่าย 1,250.50 ค่าเดินทาง",
            kind=CommandKind.EXPENSE,
            amount="1250.50",
            category="เดินทาง",
            description="ค่าเดินทาง",
            transaction_date=date(2026, 9, 1),
        )
        self.assertEqual(command.inference_rule, "explicit.จ่าย")

    def test_explicit_future_date_remains_supported(self) -> None:
        self.assert_transaction(
            "จ่าย 50 2026-09-15",
            kind=CommandKind.EXPENSE,
            amount="50",
            category="อื่นๆ",
            description="ไม่ระบุรายการ",
            transaction_date=date(2026, 9, 15),
        )
        self.assert_transaction(
            "เงินเดือน 2026-09-15 20000",
            kind=CommandKind.INCOME,
            amount="20000",
            category="เงินเดือน",
            description="เงินเดือน",
            transaction_date=date(2026, 9, 15),
        )

    def test_descriptive_not_phrases_remain_transactions(self) -> None:
        self.assert_transaction(
            "ซื้อข้าวไม่เผ็ด 50",
            kind=CommandKind.EXPENSE,
            amount="50",
            category="อาหาร",
            description="ข้าวไม่เผ็ด",
        )
        self.assert_transaction(
            "จ่ายค่าเน็ตไม่รวมภาษี 599",
            kind=CommandKind.EXPENSE,
            amount="599",
            category="บิลและบริการ",
            description="ค่าเน็ตไม่รวมภาษี",
        )
        self.assert_transaction(
            "จ่าย 50 บาท ร้านร้อยบาท",
            kind=CommandKind.EXPENSE,
            amount="50",
            category="อื่นๆ",
            description="ร้านร้อยบาท",
        )

    def test_same_direction_linked_clause_after_currency_remains_valid(self) -> None:
        command = parse_command("จ่ายข้าวแล้ว 50 บาท ซื้อหนังสือ", now=TODAY)
        self.assertIsInstance(command, TransactionCommand)
        self.assertEqual(command.kind, CommandKind.EXPENSE)

    def test_thai_buddhist_date(self) -> None:
        command = parse_command("ข้าว 80 13/09/2569", now=TODAY)
        self.assertIsInstance(command, TransactionCommand)
        self.assertEqual(command.transaction_date, date(2026, 9, 13))

    def test_aware_datetime_is_converted_to_bangkok(self) -> None:
        utc_time = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
        command = parse_command("จ่าย 20", now=utc_time)
        self.assertIsInstance(command, TransactionCommand)
        self.assertEqual(command.transaction_date, date(2026, 9, 14))


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

    def test_amount_only_does_not_guess_type(self) -> None:
        for text in ("500", "50.50", "1,200", "๑๐๐"):
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(command.reason, "กรุณาระบุว่าเป็นรายรับหรือรายจ่าย")

    def test_known_item_without_amount_asks_for_amount(self) -> None:
        command = self.assert_unresolved("ข้าว", CommandKind.AMBIGUOUS)
        self.assertEqual(command.reason, "กรุณาระบุจำนวนเงินของ ข้าว")

    def test_negative_amount_is_not_silently_made_positive(self) -> None:
        for text in ("จ่าย -50 ข้าว", "ข้าว −50"):
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(command.reason, "จำนวนเงินต้องมากกว่า 0")

    def test_zero_and_malformed_amounts_are_rejected(self) -> None:
        examples = {
            "ข้าว 0": "จำนวนเงินต้องมากกว่า 0",
            "ข้าว 50.999": "จำนวนเงินมีทศนิยมได้ไม่เกิน 2 ตำแหน่ง",
            "ข้าว 1,20": "รูปแบบจำนวนเงินไม่ถูกต้อง",
            "ข้าว .50 100": "รูปแบบจำนวนเงินไม่ถูกต้อง",
            "ข้าว 1.2.3 50": "รูปแบบจำนวนเงินไม่ถูกต้อง",
            "ข้าว ,100": "รูปแบบจำนวนเงินไม่ถูกต้อง",
            "ข้าว 1,000.": "รูปแบบจำนวนเงินไม่ถูกต้อง",
            "ข้าว ๑,๐๐๐.": "รูปแบบจำนวนเงินไม่ถูกต้อง",
            "ข้าว .1.2 50": "รูปแบบจำนวนเงินไม่ถูกต้อง",
            "ข้าว ..50 100": "รูปแบบจำนวนเงินไม่ถูกต้อง",
            "ข้าว ,1,000": "รูปแบบจำนวนเงินไม่ถูกต้อง",
        }
        for text, reason in examples.items():
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(command.reason, reason)

    def test_rejects_two_amounts_with_specific_reason(self) -> None:
        command = self.assert_unresolved("จ่าย 50 อาหาร 20", CommandKind.AMBIGUOUS)
        self.assertEqual(command.reason, "พบจำนวนเงินมากกว่าหนึ่งค่า")

    def test_rejects_both_transaction_types(self) -> None:
        examples = (
            "รับแล้วจ่าย 500",
            "จ่าย แล้วรับ 500",
            "จ่ายค่าข้าวแล้วรับเงินคืน 500",
            "ซื้อข้าวแล้วขาย 50",
            "ขายข้าวแล้วจ่าย 50",
            "จ่ายข้าวแล้วได้เงิน 50",
            "ได้เงินแล้วจ่ายข้าว 50",
            "แม่ให้แล้วจ่าย 500",
            "เงินเดือนเข้าแล้วซื้อข้าว 50",
            "ซื้อข้าวแล้วไปขาย 50",
            "ขายข้าวแล้วไปซื้อ 50",
            "จ่ายข้าวแล้วก็ได้เงินคืน 50",
            "จ่ายข้าวแล้วได้ค่าขนม 50",
            "จ่ายข้าวแล้วเงินเดือน 50",
            "จ่ายข้าวแล้วโบนัส 500",
            "เติมน้ำมันแล้วได้เงิน 1000",
            "ดูหนังแล้วได้เงิน 300",
            "เรียนพิเศษแล้วได้เงิน 500",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assert_unresolved(text, CommandKind.AMBIGUOUS)

    def test_currency_before_linked_action_does_not_hide_conflict(self) -> None:
        examples = (
            "จ่ายข้าวแล้ว 50 บาท ได้เงิน",
            "ได้เงินแล้ว 50 บาท จ่ายข้าว",
            "จ่ายข้าวแล้ว ๕๐ บาท รับเงิน",
        )
        for text in examples:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "พบทั้งรายรับและรายจ่ายในข้อความเดียวกัน",
                )

    def test_attached_currency_before_linked_action_does_not_hide_conflict(self) -> None:
        examples = (
            "จ่ายข้าวแล้ว 50 บาทได้เงิน",
            "ได้เงินแล้ว 50 บาทจ่ายข้าว",
            "จ่ายข้าวแล้ว50บาทได้เงิน",
            "จ่ายข้าวและ50บาทได้รับโบนัส",
            "ได้เงินจากนั้น๕๐บาทจ่ายข้าว",
        )
        for text in examples:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "พบทั้งรายรับและรายจ่ายในข้อความเดียวกัน",
                )

    def test_punctuation_around_amount_does_not_hide_linked_conflict(self) -> None:
        examples = (
            "จ่ายข้าวแล้ว (50 บาท) ได้เงิน",
            "จ่ายข้าวแล้ว (50) บาท ได้เงิน",
            "จ่ายข้าวแล้ว50บาท,ได้เงิน",
            "ได้เงินแล้ว50บาท;จ่ายข้าว",
            "ได้เงินแล้ว [๕๐ บาท] จ่ายข้าว",
            "จ่ายข้าวแล้ว（50 บาท）ได้เงิน",
        )
        for text in examples:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "พบทั้งรายรับและรายจ่ายในข้อความเดียวกัน",
                )

    def test_punctuation_wrapped_linked_actions_remain_conflicts(self) -> None:
        examples = (
            "จ่ายข้าวแล้ว (ได้เงิน 50 บาท)",
            "ได้เงินแล้ว (จ่ายข้าว 50 บาท)",
            "จ่ายข้าวแล้ว (ได้เงิน) 50 บาท",
            "จ่ายข้าวแล้ว[ได้เงิน 50 บาท]",
            "ได้เงินแล้ว【จ่ายข้าว 50 บาท】",
            "จ่ายข้าวแล้ว（ได้เงิน 50 บาท）",
            "จ่ายข้าวแล้ว,ได้เงิน 50 บาท",
            "ได้เงินแล้ว; [จ่ายข้าว 50 บาท]",
        )
        for text in examples:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "พบทั้งรายรับและรายจ่ายในข้อความเดียวกัน",
                )

    def test_unknown_text(self) -> None:
        self.assert_unresolved("สวัสดี", CommandKind.UNKNOWN)

    def test_invalid_date(self) -> None:
        self.assert_unresolved("จ่าย 50 อาหาร 31/02/2026", CommandKind.AMBIGUOUS)

    def test_negated_and_future_transactions_are_not_recorded(self) -> None:
        examples = (
            "ไม่ได้ซื้อข้าว 50",
            "ไม่ได้จ่ายค่าเน็ต 599",
            "ไม่ได้รับเงินเดือน 20000",
            "ไม่ขายข้าว 50",
            "ไม่ได้ขายข้าว 50",
            "ไม่ ได้ขายข้าว 50",
            "ไม่เคยซื้อข้าว 50",
            "ไม่ ได้ ซื้อข้าว 50",
            "ไม่ได้เสียโบนัส 500",
            "ไม่ได้สอนพิเศษ 500",
            "จะขายข้าว 50",
            "จะ ซื้อข้าว 50",
            "จะเสียโบนัส 500",
            "จะสอนพิเศษ 500",
            "จะไปซื้อข้าว 50",
            "วางแผนขายข้าว 50",
            "เดือนหน้าซื้อข้าว 50",
            "พรุ่งนี้จะซื้อข้าว 50",
            "เงินเดือนยังไม่เข้า 20000",
            "เงินเดือนจะเข้า 20000",
            "เงินเดือนไม่เข้า 20000",
            "เงินเดือนกำลังจะเข้า 20000",
            "รายรับยังไม่เข้า 500",
            "ขายไม่ได้ 500",
            "ขายไม่ออก 500",
            "สอนพิเศษไม่ได้ 500",
            "เงินเดือนยังไม่ออก 20000",
            "เงินเดือนจะออก 20000",
            "จะเติมน้ำมัน 1000",
            "ไม่ได้เติมน้ำมัน 1000",
            "เติมน้ำมันไม่ได้ 1000",
            "จะดูหนัง 300",
            "จะเรียนพิเศษ 500",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assert_unresolved(text, CommandKind.AMBIGUOUS)

    def test_interrupted_negated_transactions_are_not_recorded(self) -> None:
        examples = (
            "ขายข้าวไม่ได้ 50",
            "ขายข้าวไม่ออก 50",
            "จ่ายค่าเน็ตไม่ได้ 599",
            "ซื้อข้าวไม่ได้ 50",
            "เงินเดือน 20000 ยังไม่เข้า",
            "เงินเดือนวันนี้ยังไม่เข้า 20000",
        )
        for text in examples:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "ข้อความนี้เป็นการปฏิเสธ จึงยังไม่บันทึก",
                )

    def test_terminal_negation_allows_bounded_trailing_material(self) -> None:
        examples = (
            "ขายข้าวไม่ได้ครับ 50",
            "ขายข้าวไม่ออกเลย 50",
            "จ่ายค่าเน็ตไม่ได้ค่ะ 599",
            "ขายข้าวไม่ได้! 50",
            "ซื้อหนังสือไม่ได้ครับ! 80",
            "ขายของไม่ออกนะ... 100",
            "จ่ายค่าหอไม่ได้จ้า 5000",
            "ซื้อข้าวไม่ได้คะ? 50",
        )
        for text in examples:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "ข้อความนี้เป็นการปฏิเสธ จึงยังไม่บันทึก",
                )

    def test_strong_negation_fails_closed_with_unknown_trailing_text(self) -> None:
        examples = (
            "ขายข้าวไม่ได้หรอก 50",
            "จ่ายค่าเน็ตไม่ได้หรอกค่ะ 599",
            "ขายข้าวไม่ออกเลยจริงๆ 50",
            "ซื้อหนังสือไม่ได้มั้ง 80",
            "จ่ายค่าหอไม่ได้อะนะ 5000",
            "ขายของไม่ออกจริงๆนะ 100",
            "ขายข้าวไม่ได้...หรอกนะ! 50",
            "จ่ายค่าไฟไม่ได้(หรอก) 500",
        )
        for text in examples:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "ข้อความนี้เป็นการปฏิเสธ จึงยังไม่บันทึก",
                )

    def test_interrupted_future_transactions_are_not_recorded(self) -> None:
        examples = (
            "เงินเดือน 20000 จะเข้า",
            "เงินเดือน 2026-09-15 จะเข้า 20000",
        )
        for text in examples:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "ข้อความนี้ดูเป็นรายการที่ยังไม่เกิดขึ้น จึงยังไม่บันทึก",
                )

    def test_attached_currency_does_not_hide_transaction_intent(self) -> None:
        negated = (
            "เงินเดือน 20000 บาทยังไม่เข้า",
            "เงินเดือน ๒๐๐๐๐บาทยังไม่เข้า",
        )
        for text in negated:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "ข้อความนี้เป็นการปฏิเสธ จึงยังไม่บันทึก",
                )

        future = (
            "เงินเดือน 20000 บาทจะเข้า",
            "โบนัสวันนี้ 500บาทจะเข้า",
        )
        for text in future:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "ข้อความนี้ดูเป็นรายการที่ยังไม่เกิดขึ้น จึงยังไม่บันทึก",
                )

    def test_punctuation_around_amount_does_not_hide_transaction_intent(self) -> None:
        negated = (
            "ขายข้าวไม่ได้ครับ (50 บาท)",
            "เงินเดือน[๒๐๐๐๐บาท]ยังไม่เข้า",
        )
        for text in negated:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "ข้อความนี้เป็นการปฏิเสธ จึงยังไม่บันทึก",
                )

        future = (
            "เงินเดือน (20000 บาท) จะเข้า",
            "เงินเดือน (20000) บาท จะเข้า",
            "เงินเดือน【20000】บาทจะเข้า",
            "โบนัส（500 บาท）จะเข้า",
        )
        for text in future:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "ข้อความนี้ดูเป็นรายการที่ยังไม่เกิดขึ้น จึงยังไม่บันทึก",
                )

    def test_punctuation_wrapped_salary_intent_remains_unresolved(self) -> None:
        negated = (
            "เงินเดือน (ยังไม่เข้า 20000 บาท)",
            "เงินเดือน[ยังไม่เข้า 20000 บาท]",
            "เงินเดือน【ยังไม่เข้า 20000 บาท】",
            "เงินเดือน;[ยังไม่เข้า 20000 บาท]",
        )
        for text in negated:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "ข้อความนี้เป็นการปฏิเสธ จึงยังไม่บันทึก",
                )

        future = (
            "เงินเดือน (จะเข้า 20000 บาท)",
            "เงินเดือน[จะเข้า 20000 บาท]",
            "เงินเดือน（จะเข้า 20000 บาท）",
            "เงินเดือน,จะเข้า 20000 บาท",
        )
        for text in future:
            with self.subTest(text=text):
                command = self.assert_unresolved(text, CommandKind.AMBIGUOUS)
                self.assertEqual(
                    command.reason,
                    "ข้อความนี้ดูเป็นรายการที่ยังไม่เกิดขึ้น จึงยังไม่บันทึก",
                )

    def test_analysis_punctuation_normalization_preserves_descriptions(self) -> None:
        examples = (
            (
                "จ่ายกาแฟ(เย็น),แก้วใหญ่ 50",
                "50",
                "อาหาร",
                "กาแฟ(เย็น),แก้วใหญ่",
            ),
            (
                "จ่ายค่าเน็ต[บ้าน];รอบเดือน 599",
                "599",
                "บิลและบริการ",
                "ค่าเน็ต[บ้าน];รอบเดือน",
            ),
        )
        for text, amount, category, description in examples:
            with self.subTest(text=text):
                command = parse_command(text, now=TODAY)
                self.assertIsInstance(command, TransactionCommand)
                self.assertEqual(command.kind, CommandKind.EXPENSE)
                self.assertEqual(command.amount, Decimal(amount))
                self.assertEqual(command.category, category)
                self.assertEqual(command.description, description)

    def test_unknown_transfer_direction_is_not_guessed(self) -> None:
        command = self.assert_unresolved("โอน 500", CommandKind.AMBIGUOUS)
        self.assertEqual(command.reason, "กรุณาระบุว่าเป็นรายรับหรือรายจ่าย")


if __name__ == "__main__":
    unittest.main()
