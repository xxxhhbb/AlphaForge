"""Append-only SQLite paper journal with deterministic replay and fill idempotency."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import fail, require_aware_datetime, require_finite_number, require_text
from ..data import canonical_content_hash
from .models import JournalEvent, JournalEventType, JournalState, PaperFill, PaperOrderBatch


JOURNAL_SCHEMA_VERSION = "paper-journal/v1"
_SQLITE_HEADER = b"SQLite format 3\x00"


EventSpec = tuple[
    JournalEventType,
    datetime,
    str,
    str,
    Decimal,
    Decimal,
    str | None,
    Decimal | None,
]


class PaperJournal:
    """Financial truth for the paper slice; inserts only, replay validates hash chain."""

    def __init__(
        self,
        path: str | Path,
        *,
        initial_cash: Decimal,
        opened_at: datetime,
    ) -> None:
        require_finite_number(initial_cash, "initial_cash")
        require_aware_datetime(opened_at, "opened_at")
        if initial_cash <= 0:
            fail(FailureCode.INVALID_VALUE, "initial_cash must be positive")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._check_physical_file()
        try:
            with self._connect() as connection:
                self._initialize_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                self._initialize_metadata(connection, initial_cash)
                count = int(connection.execute("SELECT COUNT(*) FROM journal_events").fetchone()[0])
                if count == 0:
                    source_hash = canonical_content_hash(
                        {"schema": JOURNAL_SCHEMA_VERSION, "initial_cash": initial_cash}
                    )
                    self._append_specs(
                        connection,
                        (
                            (
                                JournalEventType.OPENING_CASH,
                                opened_at,
                                "journal-opening",
                                source_hash,
                                initial_cash,
                                Decimal("0"),
                                None,
                                None,
                            ),
                        ),
                    )
                connection.commit()
        except sqlite3.DatabaseError as exc:
            raise ContractValidationError(
                FailureCode.LEDGER_CORRUPTION,
                "paper journal initialization failed closed",
            ) from exc
        self.replay()

    def _check_physical_file(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        try:
            with self.path.open("rb") as handle:
                header = handle.read(len(_SQLITE_HEADER))
        except OSError as exc:
            raise ContractValidationError(
                FailureCode.LEDGER_CORRUPTION,
                "paper journal cannot be read",
            ) from exc
        if header != _SQLITE_HEADER:
            fail(FailureCode.LEDGER_CORRUPTION, "paper journal SQLite header is invalid")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS journal_events (
                sequence INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                event_time TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                amount TEXT NOT NULL,
                quantity TEXT NOT NULL,
                symbol TEXT,
                price TEXT,
                previous_event_hash TEXT,
                event_hash TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS processed_fills (
                fill_id TEXT PRIMARY KEY,
                fill_hash TEXT NOT NULL UNIQUE
            );
            CREATE TRIGGER IF NOT EXISTS journal_events_no_update
            BEFORE UPDATE ON journal_events BEGIN
                SELECT RAISE(ABORT, 'journal_events are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS journal_events_no_delete
            BEFORE DELETE ON journal_events BEGIN
                SELECT RAISE(ABORT, 'journal_events are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS processed_fills_no_update
            BEFORE UPDATE ON processed_fills BEGIN
                SELECT RAISE(ABORT, 'processed_fills are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS processed_fills_no_delete
            BEFORE DELETE ON processed_fills BEGIN
                SELECT RAISE(ABORT, 'processed_fills are append-only');
            END;
            """
        )

    @staticmethod
    def _initialize_metadata(connection: sqlite3.Connection, initial_cash: Decimal) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', ?)",
            (JOURNAL_SCHEMA_VERSION,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('initial_cash', ?)",
            (str(initial_cash),),
        )
        values = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
        if values.get("schema_version") != JOURNAL_SCHEMA_VERSION:
            fail(FailureCode.LEDGER_CORRUPTION, "paper journal schema version mismatch")
        if Decimal(values.get("initial_cash", "NaN")) != initial_cash:
            fail(FailureCode.LEDGER_CORRUPTION, "paper journal opening cash mismatch")

    @staticmethod
    def _row_to_event(row: tuple[object, ...]) -> JournalEvent:
        (
            sequence,
            event_id,
            event_type,
            event_time,
            source_id,
            source_hash,
            amount,
            quantity,
            symbol,
            price,
            previous_event_hash,
            _event_hash,
        ) = row
        parsed_time = datetime.fromisoformat(str(event_time))
        return JournalEvent(
            event_id=str(event_id),
            sequence=int(sequence),
            event_type=JournalEventType(str(event_type)),
            event_time=parsed_time,
            source_id=str(source_id),
            source_hash=str(source_hash),
            amount=Decimal(str(amount)),
            quantity=Decimal(str(quantity)),
            symbol=None if symbol is None else str(symbol),
            price=None if price is None else Decimal(str(price)),
            previous_event_hash=(
                None if previous_event_hash is None else str(previous_event_hash)
            ),
        )

    def events(self) -> tuple[JournalEvent, ...]:
        self._check_physical_file()
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT sequence, event_id, event_type, event_time, source_id,
                           source_hash, amount, quantity, symbol, price,
                           previous_event_hash, event_hash
                    FROM journal_events ORDER BY sequence
                    """
                ).fetchall()
        except (sqlite3.DatabaseError, ValueError) as exc:
            raise ContractValidationError(
                FailureCode.LEDGER_CORRUPTION,
                "paper journal read failed closed",
            ) from exc
        events: list[JournalEvent] = []
        previous_hash: str | None = None
        previous_time: datetime | None = None
        for expected_sequence, row in enumerate(rows, start=1):
            try:
                event = self._row_to_event(row)
            except (ValueError, TypeError, ContractValidationError) as exc:
                raise ContractValidationError(
                    FailureCode.LEDGER_CORRUPTION,
                    "paper journal event cannot be decoded",
                ) from exc
            stored_hash = str(row[-1])
            if event.sequence != expected_sequence:
                fail(FailureCode.LEDGER_CORRUPTION, "paper journal sequence gap")
            if event.previous_event_hash != previous_hash:
                fail(FailureCode.LEDGER_CORRUPTION, "paper journal hash-chain link mismatch")
            if event.artifact_hash != stored_hash:
                fail(FailureCode.LEDGER_CORRUPTION, "paper journal event hash mismatch")
            if previous_time is not None and event.event_time < previous_time:
                fail(FailureCode.LEDGER_CORRUPTION, "paper journal time moved backward")
            events.append(event)
            previous_hash = stored_hash
            previous_time = event.event_time
        if not events or events[0].event_type is not JournalEventType.OPENING_CASH:
            fail(FailureCode.LEDGER_CORRUPTION, "paper journal lacks opening cash event")
        return tuple(events)

    @staticmethod
    def _last_row(connection: sqlite3.Connection) -> tuple[int, str | None, datetime | None]:
        row = connection.execute(
            "SELECT sequence, event_hash, event_time FROM journal_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0, None, None
        return int(row[0]), str(row[1]), datetime.fromisoformat(str(row[2]))

    def _append_specs(
        self, connection: sqlite3.Connection, specs: Iterable[EventSpec]
    ) -> tuple[JournalEvent, ...]:
        sequence, previous_hash, previous_time = self._last_row(connection)
        appended: list[JournalEvent] = []
        for spec in specs:
            (
                event_type,
                event_time,
                source_id,
                source_hash,
                amount,
                quantity,
                symbol,
                price,
            ) = spec
            if previous_time is not None and event_time < previous_time:
                fail(FailureCode.INVALID_TIME_ORDER, "journal append time moved backward")
            sequence += 1
            event_material_hash = canonical_content_hash(
                {
                    "sequence": sequence,
                    "event_type": event_type.value,
                    "event_time": event_time,
                    "source_id": source_id,
                    "source_hash": source_hash,
                    "symbol": symbol,
                    "amount": amount,
                    "quantity": quantity,
                    "price": price,
                    "previous_event_hash": previous_hash,
                }
            )
            event = JournalEvent(
                event_id="journal-" + event_material_hash.removeprefix("sha256:")[:24],
                sequence=sequence,
                event_type=event_type,
                event_time=event_time,
                source_id=source_id,
                source_hash=source_hash,
                amount=amount,
                quantity=quantity,
                symbol=symbol,
                price=price,
                previous_event_hash=previous_hash,
            )
            connection.execute(
                """
                INSERT INTO journal_events(
                    sequence, event_id, event_type, event_time, source_id,
                    source_hash, amount, quantity, symbol, price,
                    previous_event_hash, event_hash
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.sequence,
                    event.event_id,
                    event.event_type.value,
                    event.event_time.isoformat(),
                    event.source_id,
                    event.source_hash,
                    str(event.amount),
                    str(event.quantity),
                    event.symbol,
                    None if event.price is None else str(event.price),
                    event.previous_event_hash,
                    event.artifact_hash,
                ),
            )
            appended.append(event)
            previous_hash = event.artifact_hash
            previous_time = event.event_time
        return tuple(appended)

    @staticmethod
    def _position_after(
        quantity: Decimal,
        average_cost: Decimal,
        trade_quantity: Decimal,
        trade_price: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        new_quantity = quantity + trade_quantity
        realized = Decimal("0")
        if quantity == 0 or quantity * trade_quantity > 0:
            total = abs(quantity) + abs(trade_quantity)
            new_average = (
                (abs(quantity) * average_cost + abs(trade_quantity) * trade_price) / total
            )
        else:
            closed = min(abs(quantity), abs(trade_quantity))
            direction = Decimal("1") if quantity > 0 else Decimal("-1")
            realized = closed * (trade_price - average_cost) * direction
            if new_quantity == 0:
                new_average = Decimal("0")
            elif quantity * new_quantity > 0:
                new_average = average_cost
            else:
                new_average = trade_price
        return new_quantity, new_average, realized

    def replay(self) -> JournalState:
        events = self.events()
        opening_cash = Decimal("0")
        cash = Decimal("0")
        positions: dict[str, Decimal] = {}
        average: dict[str, Decimal] = {}
        latest: dict[str, Decimal] = {}
        realized_by_symbol: dict[str, Decimal] = {}
        unrealized_by_symbol: dict[str, Decimal] = {}
        fees = borrow = financing = Decimal("0")
        realized = unrealized = Decimal("0")
        spread = impact = Decimal("0")
        halted = False
        for event in events:
            if event.event_type is JournalEventType.OPENING_CASH:
                opening_cash += event.amount
                cash += event.amount
            elif event.event_type is JournalEventType.CASH:
                cash += event.amount
            elif event.event_type is JournalEventType.POSITION:
                assert event.symbol is not None and event.price is not None
                old_quantity = positions.get(event.symbol, Decimal("0"))
                old_average = average.get(event.symbol, Decimal("0"))
                new_quantity, new_average, _ = self._position_after(
                    old_quantity, old_average, event.quantity, event.price
                )
                positions[event.symbol] = new_quantity
                average[event.symbol] = new_average
                latest[event.symbol] = event.price
            elif event.event_type is JournalEventType.FEES:
                fees += event.amount
            elif event.event_type is JournalEventType.BORROW:
                borrow += event.amount
            elif event.event_type is JournalEventType.FINANCING:
                financing += event.amount
            elif event.event_type is JournalEventType.REALIZED_PNL:
                realized += event.amount
                if event.symbol is not None:
                    realized_by_symbol[event.symbol] = (
                        realized_by_symbol.get(event.symbol, Decimal("0")) + event.amount
                    )
            elif event.event_type is JournalEventType.UNREALIZED_PNL:
                unrealized += event.amount
                if event.symbol is not None:
                    unrealized_by_symbol[event.symbol] = (
                        unrealized_by_symbol.get(event.symbol, Decimal("0")) + event.amount
                    )
                    if event.price is not None:
                        latest[event.symbol] = event.price
            elif event.event_type is JournalEventType.SPREAD_COST:
                spread += event.amount
            elif event.event_type is JournalEventType.IMPACT_COST:
                impact += event.amount
            elif event.event_type is JournalEventType.HALT:
                halted = True
        return JournalState(
            opening_cash=opening_cash,
            cash=cash,
            positions=positions,
            average_cost=average,
            latest_prices=latest,
            fees=fees,
            borrow=borrow,
            financing=financing,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            spread_cost=spread,
            impact_cost=impact,
            realized_by_symbol=realized_by_symbol,
            unrealized_by_symbol=unrealized_by_symbol,
            event_count=len(events),
            last_event_hash=events[-1].artifact_hash,
            halted=halted,
        )

    def post_fill(self, *, paper_fill: PaperFill, batch: PaperOrderBatch) -> JournalState:
        if not isinstance(paper_fill, PaperFill) or not isinstance(batch, PaperOrderBatch):
            fail(FailureCode.INVALID_VALUE, "typed PaperFill and PaperOrderBatch are required")
        fill = paper_fill.fill
        if fill.order_plan_hash != batch.order_plan.artifact_hash:
            fail(FailureCode.INVALID_HASH, "fill does not bind supplied OrderPlan")
        order = next(
            (
                item
                for item in batch.order_plan.orders
                if item.client_order_id == fill.client_order_id
            ),
            None,
        )
        if order is None or order.symbol != fill.symbol or order.side != paper_fill.side:
            fail(FailureCode.AMBIGUOUS_EXECUTION_STATE, "fill/order identity is ambiguous")
        with self._lock:
            state = self.replay()
            if state.halted:
                fail(FailureCode.EXECUTION_HALTED, "journal is halted")
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    existing = connection.execute(
                        "SELECT fill_hash FROM processed_fills WHERE fill_id = ?",
                        (fill.fill_id,),
                    ).fetchone()
                    if existing is not None:
                        connection.rollback()
                        if str(existing[0]) == fill.artifact_hash:
                            return self.replay()
                        self.append_halt(
                            reason="DUPLICATE_FILL_CONFLICT",
                            event_time=fill.available_at,
                            source_hash=fill.artifact_hash,
                        )
                        raise ContractValidationError(
                            FailureCode.DUPLICATE_FILL_CONFLICT,
                            "same fill_id has conflicting content",
                        )
                    signed_quantity = (
                        paper_fill.filled_quantity
                        if paper_fill.side == "BUY"
                        else -paper_fill.filled_quantity
                    )
                    old_quantity = state.positions.get(fill.symbol, Decimal("0"))
                    old_average = state.average_cost.get(fill.symbol, Decimal("0"))
                    new_quantity, new_average, realized = self._position_after(
                        old_quantity, old_average, signed_quantity, fill.price
                    )
                    prior_unrealized = state.unrealized_by_symbol.get(
                        fill.symbol, Decimal("0")
                    )
                    new_unrealized = new_quantity * (fill.price - new_average)
                    unrealized_delta = new_unrealized - prior_unrealized
                    notional = paper_fill.filled_quantity * fill.price
                    cash_delta = (
                        -notional - paper_fill.fees
                        if paper_fill.side == "BUY"
                        else notional - paper_fill.fees
                    )
                    source_id = fill.fill_id
                    source_hash = fill.artifact_hash
                    specs: tuple[EventSpec, ...] = (
                        (
                            JournalEventType.CASH,
                            fill.available_at,
                            source_id,
                            source_hash,
                            cash_delta,
                            Decimal("0"),
                            fill.symbol,
                            fill.price,
                        ),
                        (
                            JournalEventType.POSITION,
                            fill.available_at,
                            source_id,
                            source_hash,
                            Decimal("0"),
                            signed_quantity,
                            fill.symbol,
                            fill.price,
                        ),
                        (
                            JournalEventType.FEES,
                            fill.available_at,
                            source_id,
                            source_hash,
                            paper_fill.fees,
                            Decimal("0"),
                            fill.symbol,
                            fill.price,
                        ),
                        (
                            JournalEventType.SPREAD_COST,
                            fill.available_at,
                            source_id,
                            source_hash,
                            paper_fill.spread_cost,
                            Decimal("0"),
                            fill.symbol,
                            fill.price,
                        ),
                        (
                            JournalEventType.IMPACT_COST,
                            fill.available_at,
                            source_id,
                            source_hash,
                            paper_fill.impact_cost,
                            Decimal("0"),
                            fill.symbol,
                            fill.price,
                        ),
                        (
                            JournalEventType.REALIZED_PNL,
                            fill.available_at,
                            source_id,
                            source_hash,
                            realized,
                            Decimal("0"),
                            fill.symbol,
                            fill.price,
                        ),
                        (
                            JournalEventType.UNREALIZED_PNL,
                            fill.available_at,
                            source_id,
                            source_hash,
                            unrealized_delta,
                            Decimal("0"),
                            fill.symbol,
                            fill.price,
                        ),
                    )
                    self._append_specs(connection, specs)
                    connection.execute(
                        "INSERT INTO processed_fills(fill_id, fill_hash) VALUES(?, ?)",
                        (fill.fill_id, fill.artifact_hash),
                    )
                    connection.commit()
            except sqlite3.DatabaseError as exc:
                raise ContractValidationError(
                    FailureCode.LEDGER_CORRUPTION,
                    "paper fill journal transaction failed closed",
                ) from exc
            return self.replay()

    def mark_to_market(
        self,
        *,
        marks: Mapping[str, Decimal],
        event_time: datetime,
    ) -> JournalState:
        require_aware_datetime(event_time, "event_time")
        state = self.replay()
        if state.halted:
            fail(FailureCode.EXECUTION_HALTED, "journal is halted")
        if set(state.positions) - set(marks):
            fail(FailureCode.RECONCILIATION_FAILED, "marks missing held symbols")
        source_hash = canonical_content_hash({"marks": marks, "event_time": event_time})
        source_id = "mark-" + source_hash.removeprefix("sha256:")[:20]
        specs: list[EventSpec] = []
        for symbol in sorted(state.positions):
            mark = Decimal(marks[symbol])
            require_finite_number(mark, f"marks.{symbol}")
            if mark <= 0:
                fail(FailureCode.INVALID_VALUE, "mark prices must be positive")
            new_unrealized = state.positions[symbol] * (
                mark - state.average_cost.get(symbol, Decimal("0"))
            )
            delta = new_unrealized - state.unrealized_by_symbol.get(
                symbol, Decimal("0")
            )
            specs.append(
                (
                    JournalEventType.UNREALIZED_PNL,
                    event_time,
                    source_id,
                    source_hash,
                    delta,
                    Decimal("0"),
                    symbol,
                    mark,
                )
            )
        if not specs:
            return state
        with self._lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._append_specs(connection, specs)
                    connection.commit()
            except sqlite3.DatabaseError as exc:
                raise ContractValidationError(
                    FailureCode.LEDGER_CORRUPTION,
                    "mark-to-market append failed closed",
                ) from exc
        return self.replay()

    def post_daily_costs(
        self,
        *,
        marks: Mapping[str, Decimal],
        borrow_bps: Decimal,
        financing_bps: Decimal,
        event_time: datetime,
    ) -> JournalState:
        require_aware_datetime(event_time, "event_time")
        for name, value in (("borrow_bps", borrow_bps), ("financing_bps", financing_bps)):
            require_finite_number(value, name)
            if value < 0:
                fail(FailureCode.INVALID_VALUE, f"{name} cannot be negative")
        state = self.replay()
        if set(state.positions) - set(marks):
            fail(FailureCode.RECONCILIATION_FAILED, "cost marks missing held symbols")
        borrow_cost = sum(
            (
                abs(quantity) * Decimal(marks[symbol]) * borrow_bps / Decimal("10000")
                for symbol, quantity in state.positions.items()
                if quantity < 0
            ),
            Decimal("0"),
        )
        financing_cost = (
            abs(state.cash) * financing_bps / Decimal("10000")
            if state.cash < 0
            else Decimal("0")
        )
        source_hash = canonical_content_hash(
            {
                "marks": marks,
                "borrow_bps": borrow_bps,
                "financing_bps": financing_bps,
                "event_time": event_time,
            }
        )
        source_id = "cost-" + source_hash.removeprefix("sha256:")[:20]
        specs: list[EventSpec] = []
        if borrow_cost > 0:
            specs.extend(
                (
                    (
                        JournalEventType.BORROW,
                        event_time,
                        source_id,
                        source_hash,
                        borrow_cost,
                        Decimal("0"),
                        None,
                        None,
                    ),
                    (
                        JournalEventType.CASH,
                        event_time,
                        source_id,
                        source_hash,
                        -borrow_cost,
                        Decimal("0"),
                        None,
                        None,
                    ),
                )
            )
        if financing_cost > 0:
            specs.extend(
                (
                    (
                        JournalEventType.FINANCING,
                        event_time,
                        source_id,
                        source_hash,
                        financing_cost,
                        Decimal("0"),
                        None,
                        None,
                    ),
                    (
                        JournalEventType.CASH,
                        event_time,
                        source_id,
                        source_hash,
                        -financing_cost,
                        Decimal("0"),
                        None,
                        None,
                    ),
                )
            )
        if not specs:
            return state
        with self._lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._append_specs(connection, specs)
                    connection.commit()
            except sqlite3.DatabaseError as exc:
                raise ContractValidationError(
                    FailureCode.LEDGER_CORRUPTION,
                    "daily financing append failed closed",
                ) from exc
        return self.replay()

    def append_halt(
        self,
        *,
        reason: str,
        event_time: datetime,
        source_hash: str,
    ) -> JournalState:
        require_text(reason, "reason")
        require_aware_datetime(event_time, "event_time")
        state = self.replay()
        if state.halted:
            return state
        reason_hash = canonical_content_hash({"reason": reason, "source_hash": source_hash})
        source_id = "halt-" + reason_hash.removeprefix("sha256:")[:20]
        with self._lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._append_specs(
                        connection,
                        (
                            (
                                JournalEventType.HALT,
                                event_time,
                                source_id,
                                source_hash,
                                Decimal("0"),
                                Decimal("0"),
                                None,
                                None,
                            ),
                        ),
                    )
                    connection.commit()
            except sqlite3.DatabaseError as exc:
                raise ContractValidationError(
                    FailureCode.LEDGER_CORRUPTION,
                    "halt append failed closed",
                ) from exc
        return self.replay()
