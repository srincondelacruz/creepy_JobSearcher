"""Telegram bot with conversation flows for job notifications and questionnaire generation."""
import asyncio
import os
import textwrap
from typing import Optional

from loguru import logger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from modules.generator.responder import Responder
from modules.storage.database import Database


# ── Conversation states ───────────────────────────────────────────────────────
RESPOND_WAITING_OFFER = 1
RESPOND_WAITING_QUESTIONS = 2
COVER_WAITING_OFFER = 3
ANALYZE_WAITING_URL = 4


def _escape(text: str) -> str:
    """Escape MarkdownV2 special chars."""
    special = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


class JobBot:
    def __init__(
        self,
        settings: dict,
        db: Database,
        responder: Responder,
        dry_run: bool = False,
        agent_graph=None,
    ):
        token = self._resolve(settings["telegram"]["bot_token"])
        raw_ids = self._resolve(settings["telegram"].get("allowed_user_ids", ""))
        self.allowed_ids: set[int] = (
            {int(i.strip()) for i in raw_ids.split(",") if i.strip()}
            if raw_ids else set()
        )
        self.db = db
        self.responder = responder
        self.dry_run = dry_run
        self.agent_graph = agent_graph  # JobAgentGraph — enables /analizar
        self.search_fn = None           # set via schedule_daily_search — enables /buscar
        self._search_running = False
        self.notify_min_score = float(
            settings.get("scheduler", {}).get("notify_min_score", 6)
        )
        self.app = Application.builder().token(token).build()
        self._register_handlers()

    # ── Auth guard ────────────────────────────────────────────────────────────

    def _allowed(self, user_id: int) -> bool:
        return not self.allowed_ids or user_id in self.allowed_ids

    async def _guard(self, update: Update) -> bool:
        if not self._allowed(update.effective_user.id):
            await update.message.reply_text("⛔ Acceso no autorizado.")
            return False
        return True

    # ── Handler registration ──────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        app = self.app

        # Simple commands
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("ayuda", self.cmd_ayuda))
        app.add_handler(CommandHandler("buscar", self.cmd_buscar))
        app.add_handler(CommandHandler("estado", self.cmd_estado))
        app.add_handler(CallbackQueryHandler(self.on_status_button, pattern=r"^st:"))
        app.add_handler(CommandHandler("ofertas", self.cmd_ofertas))

        # /responder conversation
        app.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler("responder", self.respond_start)],
                states={
                    RESPOND_WAITING_OFFER: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self.respond_got_offer)
                    ],
                    RESPOND_WAITING_QUESTIONS: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self.respond_got_questions)
                    ],
                },
                fallbacks=[CommandHandler("cancelar", self.cancel)],
            )
        )

        # /carta conversation
        app.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler("carta", self.cover_start)],
                states={
                    COVER_WAITING_OFFER: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self.cover_got_offer)
                    ],
                },
                fallbacks=[CommandHandler("cancelar", self.cancel)],
            )
        )

        # /analizar conversation — run the LangGraph agent on a URL
        app.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler("analizar", self.analyze_start)],
                states={
                    ANALYZE_WAITING_URL: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self.analyze_got_url)
                    ],
                },
                fallbacks=[CommandHandler("cancelar", self.cancel)],
            )
        )

    # ── Commands ──────────────────────────────────────────────────────────────

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.message.reply_text(
            "👋 *Job Agent activo*\n\n"
            "Usa /ayuda para ver comandos disponibles.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_ayuda(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        text = (
            "📋 *Comandos disponibles*\n\n"
            "/analizar — Analizar una oferta por URL (encaje + cuestionario + recomendación)\n"
            "/buscar — Lanzar búsqueda manual ahora\n"
            "/responder — Generar respuestas a cuestionario de oferta\n"
            "/carta — Generar carta de presentación\n"
            "/estado — Resumen de candidaturas activas\n"
            "/ofertas — Ver últimas ofertas encontradas\n"
            "/cancelar — Cancelar operación en curso\n"
            "/ayuda — Esta ayuda"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_buscar(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if self.dry_run:
            await update.message.reply_text("🧪 *Dry-run mode* — no real search triggered.", parse_mode=ParseMode.MARKDOWN)
            return
        if self.search_fn is None:
            await update.message.reply_text("❌ Búsqueda no configurada (arranca con `agent.py serve`).")
            return
        if self._search_running:
            await update.message.reply_text("⏳ Ya hay una búsqueda en curso, espera a que termine.")
            return
        await update.message.reply_text(
            "🔍 Lanzando búsqueda manual... puede tardar 10-20 min "
            "(2 portales × 7 keywords + scoring). Te iré avisando de cada oferta nueva."
        )
        try:
            total, notified = await self._run_search_and_notify()
            await update.message.reply_text(
                f"✅ Búsqueda completada — {total} ofertas nuevas, "
                f"{notified} notificadas (encaje ≥ {self.notify_min_score:.0f}). "
                f"El resto está en la base de datos: /estado."
            )
        except Exception as e:
            logger.error(f"/buscar failed: {e}")
            await update.message.reply_text(f"❌ Error en la búsqueda: {e}")

    async def cmd_estado(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        stats = self.db.get_stats()
        by_status = stats.get("by_status", {})
        lines = [
            f"📊 *Estado de candidaturas*\n",
            f"Total ofertas: {stats['total']}",
            f"🆕 Nuevas: {by_status.get('nueva', 0)}",
            f"👀 Revisadas: {by_status.get('revisada', 0)}",
            f"📤 Aplicadas: {by_status.get('aplicada', 0)}",
            f"🗑 Descartadas: {by_status.get('descartada', 0)}",
        ]
        if stats.get("avg_score"):
            lines.append(f"\nPuntuación media de encaje: {stats['avg_score']}/10")
        if stats.get("top_jobs"):
            lines.append("\n🏆 *Top ofertas:*")
            for j in stats["top_jobs"][:3]:
                score = j.get("fit_score", "?")
                lines.append(f"  • [{j['title']} @ {j['company']}]({j['url']}) — {score}/10")
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )

    async def cmd_ofertas(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        jobs = self.db.get_jobs(status="nueva", limit=10)
        if not jobs:
            await update.message.reply_text("No hay ofertas nuevas en este momento.")
            return
        for job in jobs[:5]:
            await update.message.reply_text(
                self._format_job_message(job),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )

    # ── /responder conversation ───────────────────────────────────────────────

    async def respond_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        await update.message.reply_text(
            "📄 *Generador de respuestas*\n\n"
            "Pega el texto completo de la oferta de trabajo:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return RESPOND_WAITING_OFFER

    async def respond_got_offer(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data["offer_text"] = update.message.text
        await update.message.reply_text(
            "✅ Oferta recibida.\n\n"
            "Ahora pega las preguntas del cuestionario.\n"
            "Sepáralas con `|` o en líneas separadas:\n\n"
            "_Ejemplo:_ ¿Cuántos años de experiencia con Python?|¿Nivel de inglés?",
            parse_mode=ParseMode.MARKDOWN,
        )
        return RESPOND_WAITING_QUESTIONS

    async def respond_got_questions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        raw = update.message.text
        questions = [q.strip() for q in raw.replace("\n", "|").split("|") if q.strip()]
        offer_text = ctx.user_data.get("offer_text", "")

        await update.message.reply_text(f"⚙️ Generando {len(questions)} respuesta(s)...")
        try:
            responses = self.responder.generate_responses(offer_text, questions)
            for r in responses:
                msg = f"*❓ {r['question']}*\n\n{r['answer']}"
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Response generation error: {e}")
            await update.message.reply_text(f"❌ Error al generar respuestas: {e}")
        return ConversationHandler.END

    # ── /carta conversation ───────────────────────────────────────────────────

    async def cover_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        await update.message.reply_text(
            "✉️ *Carta de presentación*\n\nPega el texto completo de la oferta:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return COVER_WAITING_OFFER

    async def cover_got_offer(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        offer_text = update.message.text
        await update.message.reply_text("⚙️ Generando carta...")
        try:
            letter = self.responder.generate_cover_letter(offer_text)
            # Split into chunks if too long for Telegram (4096 char limit)
            for chunk in textwrap.wrap(letter, 4000, replace_whitespace=False):
                await update.message.reply_text(chunk)
        except Exception as e:
            logger.error(f"Cover letter error: {e}")
            await update.message.reply_text(f"❌ Error: {e}")
        return ConversationHandler.END

    # ── /analizar conversation (LangGraph agent) ──────────────────────────────

    async def analyze_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        if self.agent_graph is None:
            await update.message.reply_text(
                "⚠️ El agente de análisis no está disponible en esta instancia."
            )
            return ConversationHandler.END

        # Allow inline URL: /analizar https://...
        args = ctx.args if hasattr(ctx, "args") else []
        if args:
            await self._run_analysis(update, args[0].strip())
            return ConversationHandler.END

        await update.message.reply_text(
            "🔍 *Analizar oferta*\n\nPega la URL de la oferta:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ANALYZE_WAITING_URL

    async def analyze_got_url(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        url = update.message.text.strip()
        await self._run_analysis(update, url)
        return ConversationHandler.END

    async def _run_analysis(self, update: Update, url: str) -> None:
        from modules.graph.formatting import format_telegram

        if not url.lower().startswith("http"):
            await update.message.reply_text("❌ Eso no parece una URL válida (debe empezar por http).")
            return

        await update.message.reply_text(
            "⚙️ Analizando oferta... esto puede tardar ~30s (fetch + análisis + cuestionario)."
        )
        try:
            user_id = str(update.effective_user.id)
            # Graph is sync + LLM-heavy → run off the event loop
            state = await asyncio.to_thread(
                self.agent_graph.analyze_url,
                url,
                thread_id=f"tg-{user_id}",
                dry_run=False,
            )
            for chunk in format_telegram(state):
                await self._safe_reply(
                    update.message, chunk,
                    parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True,
                )
        except Exception as e:
            logger.error(f"/analizar failed: {e}")
            await update.message.reply_text(f"❌ Error analizando la oferta: {e}")

    # ── Shared ────────────────────────────────────────────────────────────────

    async def cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text("❌ Operación cancelada.")
        return ConversationHandler.END

    # ── Notification sender (called externally by scheduler) ─────────────────

    @staticmethod
    async def _safe_reply(message, text: str, **kwargs) -> None:
        """reply_text with Markdown; on parse failure, resend as plain text."""
        try:
            await message.reply_text(text, **kwargs)
        except BadRequest as e:
            if "parse entities" not in str(e).lower():
                raise
            logger.warning(f"Markdown parse failed, resending plain: {e}")
            kwargs.pop("parse_mode", None)
            await message.reply_text(text, **kwargs)

    async def _safe_send(self, chat_id: int, text: str, **kwargs) -> None:
        """bot.send_message with Markdown; on parse failure, resend as plain text."""
        try:
            await self.app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except BadRequest as e:
            if "parse entities" not in str(e).lower():
                raise
            logger.warning(f"Markdown parse failed, resending plain: {e}")
            kwargs.pop("parse_mode", None)
            await self.app.bot.send_message(chat_id=chat_id, text=text, **kwargs)

    def _notify_chat_id(self) -> str:
        return self._resolve(
            self.app.bot_data.get("chat_id", "")
            or os.environ.get("TELEGRAM_CHAT_ID", "")
        )

    async def send_alert(self, text: str) -> None:
        """Send a plain operational alert (scraper health etc.) to the configured chat."""
        chat_id = self._notify_chat_id()
        if not chat_id or self.dry_run:
            logger.info(f"[DRY-RUN] Would alert: {text}")
            return
        try:
            await self.app.bot.send_message(chat_id=int(chat_id), text=text)
        except Exception as e:
            logger.error(f"Telegram alert error: {e}")

    async def send_job_notification(self, job: dict) -> None:
        """Send a new-job notification to the configured chat."""
        chat_id = self._notify_chat_id()
        if not chat_id or self.dry_run:
            logger.info(f"[DRY-RUN] Would notify: {job.get('title')} @ {job.get('company')}")
            return
        try:
            await self._safe_send(
                int(chat_id),
                self._format_job_message(job),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
                reply_markup=self._status_keyboard(job.get("id")),
            )
        except Exception as e:
            logger.error(f"Telegram notification error: {e}")

    @staticmethod
    def _status_keyboard(job_id) -> Optional[InlineKeyboardMarkup]:
        if job_id is None:
            return None
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Aplicada", callback_data=f"st:aplicada:{job_id}"),
            InlineKeyboardButton("👀 Revisada", callback_data=f"st:revisada:{job_id}"),
            InlineKeyboardButton("🗑 Descartar", callback_data=f"st:descartada:{job_id}"),
        ]])

    async def on_status_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._allowed(query.from_user.id):
            await query.answer("⛔ No autorizado")
            return
        _, status, job_id = query.data.split(":")
        if status == "noop":
            await query.answer()
            return
        try:
            self.db.update_job(int(job_id), status=status)
        except Exception as e:
            logger.error(f"Status button failed ({query.data}): {e}")
            await query.answer("❌ Error actualizando estado")
            return
        labels = {"aplicada": "✅ Aplicada", "revisada": "👀 Revisada", "descartada": "🗑 Descartada"}
        await query.answer(f"Marcada como {status}")
        # Replace buttons with the chosen state so the message shows the decision
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([[InlineKeyboardButton(labels.get(status, status), callback_data="st:noop:0")]])
        )

    def _format_job_message(self, job: dict) -> str:
        score = job.get("fit_score")
        score_str = f"⭐ *Encaje: {score}/10*" if score else ""
        reason = (job.get("fit_reason") or "").strip()
        reason_str = f"💡 {reason[:300]}" if reason else ""
        salary = job.get("salary_raw") or "No indicado"
        location = job.get("location") or ("🌐 Remoto" if job.get("remote") else "No indicado")
        lines = [
            f"🆕 *{job['title']}*",
            f"🏢 {job.get('company', '?')}",
            f"📍 {location}",
            f"💶 {salary}",
            score_str,
            reason_str,
            f"🔗 {job.get('url', '')}",
            f"📅 Fuente: {job.get('source', '?')}",
        ]
        return "\n".join(l for l in lines if l)

    # ── Scheduled search (PTB JobQueue — runs inside the bot's event loop) ───

    def schedule_daily_search(self, search_fn, at) -> None:
        """Run search_fn daily at the given datetime.time and notify new jobs.

        search_fn: sync callable returning a list of new job dicts.
        """
        self.search_fn = search_fn

        async def _daily_search(ctx) -> None:
            logger.info("Scheduled search starting...")
            try:
                total, notified = await self._run_search_and_notify()
                logger.info(f"Scheduled search done — {total} new jobs, {notified} notified")
            except Exception as e:
                logger.error(f"Scheduled search failed: {e}")

        self.app.job_queue.run_daily(_daily_search, time=at, name="daily_search")

    async def _run_search_and_notify(self) -> tuple[int, int]:
        """Run the configured search off-loop, notify high-fit new jobs.

        Returns (total_new, notified). Jobs scoring below notify_min_score stay
        in the DB (visible via /estado) without a Telegram message; jobs with
        no score (scoring failed) are always notified.
        """
        if self.search_fn is None:
            raise RuntimeError("search_fn not configured")
        if self._search_running:
            raise RuntimeError("search already running")
        self._search_running = True
        try:
            new_jobs, warnings = await asyncio.to_thread(self.search_fn)
            for w in warnings:
                await self.send_alert(f"⚠️ {w}")
            notified = 0
            for job in new_jobs:
                score = job.get("fit_score")
                if score is not None and score < self.notify_min_score:
                    continue
                await self.send_job_notification(job)
                notified += 1
            return len(new_jobs), notified
        finally:
            self._search_running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        logger.info("Starting Telegram bot polling...")
        self.app.run_polling(drop_pending_updates=True)

    @staticmethod
    def _resolve(value: str) -> str:
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            return os.environ.get(value[2:-1], "")
        return value or ""
