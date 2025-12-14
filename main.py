import os
import logging
from datetime import datetime, timedelta
from decimal import Decimal
import asyncio
from dotenv import load_dotenv
from threading import Thread
import pytz

load_dotenv()

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

# Flask for health check endpoint (required by Render)
from flask import Flask, jsonify

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Timezone configuration
TIMEZONE = pytz.timezone('Asia/Bangkok')  # GMT+7

def get_current_time():
    """Get current time in GMT+7"""
    return datetime.now(TIMEZONE)

def get_current_date():
    """Get current date in GMT+7"""
    return get_current_time().date()

# Database configuration
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'database': os.getenv('DB_NAME', 'budget_bot'),
    'user': os.getenv('DB_USER', 'postgres'),
    'password': os.getenv('DB_PASSWORD', 'password'),
    'port': os.getenv('DB_PORT', '5432')
}

# Initialize connection pool
db_pool = SimpleConnectionPool(1, 10, **DB_CONFIG)

# Telegram Bot Token
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')

# Flask app for health check
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return {'status': 'ok', 'bot': 'running'}, 200

@flask_app.route('/health')
def health():
    return {'status': 'healthy'}, 200

@flask_app.route('/ping')
def ping_database():
    """Endpoint to ping database and keep it active"""
    try:
        conn = DatabaseManager.get_connection()
        try:
            with conn.cursor() as cur:
                # Simple query to keep database active
                cur.execute("SELECT 1")
                cur.fetchone()
                
                # Get some stats
                cur.execute("SELECT COUNT(*) FROM users")
                user_count = cur.fetchone()[0]
                
            logger.info("Database ping successful")
            return jsonify({
                'status': 'ok',
                'database': 'active',
                'timestamp': get_current_time().isoformat(),
                'users': user_count
            }), 200
        finally:
            DatabaseManager.release_connection(conn)
    except Exception as e:
        logger.error(f"Database ping failed: {e}")
        return jsonify({
            'status': 'error',
            'database': 'inactive',
            'error': str(e),
            'timestamp': get_current_time().isoformat()
        }), 500

@flask_app.route('/status')
def status():
    """Combined status endpoint for both bot and database"""
    try:
        # Check database
        conn = DatabaseManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                user_count = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM expenses")
                expense_count = cur.fetchone()[0]
            db_status = 'active'
        except Exception as e:
            db_status = f'error: {str(e)}'
            user_count = 0
            expense_count = 0
        finally:
            DatabaseManager.release_connection(conn)
        
        return jsonify({
            'bot': 'running',
            'database': db_status,
            'stats': {
                'users': user_count,
                'expenses': expense_count
            },
            'timestamp': get_current_time().isoformat()
        }), 200
    except Exception as e:
        return jsonify({
            'bot': 'running',
            'database': 'error',
            'error': str(e),
            'timestamp': get_current_time().isoformat()
        }), 500


class DatabaseManager:
    """Handles all database operations"""
    
    @staticmethod
    def get_connection():
        return db_pool.getconn()
    
    @staticmethod
    def release_connection(conn):
        db_pool.putconn(conn)
    
    @staticmethod
    def init_database():
        """Initialize database tables"""
        conn = DatabaseManager.get_connection()
        try:
            with conn.cursor() as cur:
                # Create Users table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        telegram_id BIGINT UNIQUE NOT NULL,
                        username VARCHAR(255),
                        first_name VARCHAR(255),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Create Budget table - added adjusted_daily field
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS budgets (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                        budget_per_day DECIMAL(10, 2) NOT NULL,
                        base_amount DECIMAL(10, 2) NOT NULL,
                        adjusted_daily DECIMAL(10, 2),
                        last_adjustment_date DATE,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(user_id)
                    )
                """)
                
                # Add columns if they don't exist (for existing databases)
                cur.execute("""
                    DO $$ 
                    BEGIN 
                        BEGIN
                            ALTER TABLE budgets ADD COLUMN adjusted_daily DECIMAL(10, 2);
                        EXCEPTION
                            WHEN duplicate_column THEN NULL;
                        END;
                        BEGIN
                            ALTER TABLE budgets ADD COLUMN last_adjustment_date DATE;
                        EXCEPTION
                            WHEN duplicate_column THEN NULL;
                        END;
                    END $$;
                """)
                
                # Create Expenses table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS expenses (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                        amount DECIMAL(10, 2) NOT NULL,
                        expense_date DATE NOT NULL,
                        description TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Create indexes
                cur.execute("CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, expense_date)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)")
                
                conn.commit()
                logger.info("Database initialized successfully")
        finally:
            DatabaseManager.release_connection(conn)
    
    @staticmethod
    def get_or_create_user(telegram_id, username, first_name):
        """Get existing user or create new one"""
        conn = DatabaseManager.get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM users WHERE telegram_id = %s",
                    (telegram_id,)
                )
                user = cur.fetchone()
                
                if not user:
                    cur.execute(
                        """INSERT INTO users (telegram_id, username, first_name) 
                           VALUES (%s, %s, %s) RETURNING *""",
                        (telegram_id, username, first_name)
                    )
                    user = cur.fetchone()
                    conn.commit()
                
                return user
        finally:
            DatabaseManager.release_connection(conn)
    
    @staticmethod
    def set_budget(user_id, budget_per_day, base_amount):
        """Set or update user's budget"""
        conn = DatabaseManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO budgets (user_id, budget_per_day, base_amount, adjusted_daily, last_adjustment_date, updated_at)
                       VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (user_id) 
                       DO UPDATE SET budget_per_day = %s, base_amount = %s, updated_at = CURRENT_TIMESTAMP""",
                    (user_id, budget_per_day, base_amount, budget_per_day, get_current_date(), 
                     budget_per_day, base_amount)
                )
                conn.commit()
        finally:
            DatabaseManager.release_connection(conn)
    
    @staticmethod
    def update_adjusted_daily(user_id, adjusted_daily, adjustment_date):
        """Update the adjusted daily budget"""
        conn = DatabaseManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE budgets 
                       SET adjusted_daily = %s, last_adjustment_date = %s
                       WHERE user_id = %s""",
                    (adjusted_daily, adjustment_date, user_id)
                )
                conn.commit()
        finally:
            DatabaseManager.release_connection(conn)
    
    @staticmethod
    def check_segment_reset(user_id):
        """Check if we're at a new segment and return True if budget should be reminded"""
        conn = DatabaseManager.get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT updated_at FROM budgets WHERE user_id = %s", (user_id,))
                result = cur.fetchone()
                
                if not result:
                    return False
                
                last_update = result['updated_at'].date()
                today = get_current_date()
                
                # Check if we crossed a segment boundary
                today_segment = BudgetCalculator.get_segment_info(today)['segment']
                last_segment = BudgetCalculator.get_segment_info(last_update)['segment']
                
                # Different segment means new period started
                return today_segment != last_segment or today.month != last_update.month
        finally:
            DatabaseManager.release_connection(conn)
    
    @staticmethod
    def get_budget(user_id):
        """Get user's budget"""
        conn = DatabaseManager.get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM budgets WHERE user_id = %s", (user_id,))
                return cur.fetchone()
        finally:
            DatabaseManager.release_connection(conn)
    
    @staticmethod
    def add_expense(user_id, amount, expense_date, description=""):
        """Add an expense"""
        conn = DatabaseManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO expenses (user_id, amount, expense_date, description)
                       VALUES (%s, %s, %s, %s)""",
                    (user_id, amount, expense_date, description)
                )
                conn.commit()
        finally:
            DatabaseManager.release_connection(conn)
    
    @staticmethod
    def get_expenses(user_id, start_date, end_date):
        """Get expenses within date range"""
        conn = DatabaseManager.get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """SELECT * FROM expenses 
                       WHERE user_id = %s AND expense_date BETWEEN %s AND %s
                       ORDER BY expense_date DESC, created_at DESC""",
                    (user_id, start_date, end_date)
                )
                return cur.fetchall()
        finally:
            DatabaseManager.release_connection(conn)
    
    @staticmethod
    def get_total_spent(user_id, start_date, end_date):
        """Get total spent in date range"""
        conn = DatabaseManager.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COALESCE(SUM(amount), 0) as total
                       FROM expenses 
                       WHERE user_id = %s AND expense_date BETWEEN %s AND %s""",
                    (user_id, start_date, end_date)
                )
                result = cur.fetchone()
                return Decimal(result[0]) if result else Decimal(0)
        finally:
            DatabaseManager.release_connection(conn)


class BudgetCalculator:
    """Handles budget calculations"""
    
    @staticmethod
    def get_segment_info(date):
        """Get current 10-day segment info"""
        day = date.day
        
        if day <= 10:
            segment = 1
            start_day = 1
            end_day = 10
        elif day <= 20:
            segment = 2
            start_day = 11
            end_day = 20
        else:
            segment = 3
            start_day = 21
            # Get last day of month
            next_month = date.replace(day=28) + timedelta(days=4)
            end_day = (next_month - timedelta(days=next_month.day)).day
        
        start_date = date.replace(day=start_day)
        end_date = date.replace(day=end_day)
        days_in_segment = (end_date - start_date).days + 1
        days_remaining = (end_date - date).days + 1
        
        return {
            'segment': segment,
            'start_date': start_date,
            'end_date': end_date,
            'days_in_segment': days_in_segment,
            'days_remaining': days_remaining,
            'days_passed': days_in_segment - days_remaining + 1
        }
    
    @staticmethod
    def calculate_daily_summary(user_id):
        """Calculate daily spending summary"""
        today = get_current_date()
        
        budget = DatabaseManager.get_budget(user_id)
        if not budget:
            return None
        
        total_spent_today = DatabaseManager.get_total_spent(user_id, today, today)
        
        # Use adjusted_daily if available and set today, otherwise use base budget_per_day
        current_daily_budget = budget['budget_per_day']
        if budget.get('adjusted_daily') and budget.get('last_adjustment_date') == today:
            current_daily_budget = budget['adjusted_daily']
        
        remaining_today = Decimal(current_daily_budget) - total_spent_today
        
        return {
            'date': today,
            'budget_per_day': budget['budget_per_day'],
            'current_daily_budget': current_daily_budget,
            'spent_today': total_spent_today,
            'remaining_today': remaining_today
        }
    
    @staticmethod
    def calculate_segment_summary(user_id):
        """Calculate 10-day segment summary"""
        today = get_current_date()
        segment_info = BudgetCalculator.get_segment_info(today)
        
        budget = DatabaseManager.get_budget(user_id)
        if not budget:
            return None
        
        # Use base_amount as the segment budget (user's total for the 10-day period)
        segment_budget = Decimal(budget['base_amount'])
        
        total_spent_segment = DatabaseManager.get_total_spent(
            user_id, 
            segment_info['start_date'], 
            segment_info['end_date']
        )
        remaining_segment = segment_budget - total_spent_segment
        
        # Calculate suggested daily for TOMORROW onwards (remaining days - 1 for today)
        days_remaining_from_tomorrow = segment_info['days_remaining'] - 1
        suggested_daily = remaining_segment / days_remaining_from_tomorrow if days_remaining_from_tomorrow > 0 else Decimal(0)
        
        # Also show target daily (what they should be spending ideally)
        target_daily = Decimal(budget['budget_per_day'])
        
        # Get current daily budget (adjusted or base)
        current_daily_budget = budget['budget_per_day']
        if budget.get('adjusted_daily') and budget.get('last_adjustment_date') == today:
            current_daily_budget = budget['adjusted_daily']
        
        return {
            **segment_info,
            'segment_budget': segment_budget,
            'spent_segment': total_spent_segment,
            'remaining_segment': remaining_segment,
            'suggested_daily': suggested_daily,
            'target_daily': target_daily,
            'current_daily_budget': current_daily_budget,
            'days_remaining_from_tomorrow': days_remaining_from_tomorrow
        }


# Bot Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user = update.effective_user
    db_user = DatabaseManager.get_or_create_user(
        user.id, 
        user.username, 
        user.first_name
    )
    
    keyboard = [
        [KeyboardButton("💰 Add Expense"), KeyboardButton("📊 Daily Summary")],
        [KeyboardButton("📈 Segment Summary"), KeyboardButton("⚙️ Set Budget")],
        [KeyboardButton("📋 View Expenses")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"👋 Welcome {user.first_name}!\n\n"
        "I'll help you manage your daily spending and budget.\n\n"
        "Use the buttons below or these commands:\n"
        "/setbudget <daily_amount> <base_amount> - Set your budget\n"
        "/add <amount> [description] - Add an expense\n"
        "/today - See today's summary\n"
        "/segment - See 10-day segment summary\n"
        "/expenses - View recent expenses",
        reply_markup=reply_markup
    )


async def set_budget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setbudget command"""
    user = update.effective_user
    db_user = DatabaseManager.get_or_create_user(user.id, user.username, user.first_name)
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage: /setbudget <daily_amount> <segment_total>\n"
            "Example: /setbudget 5 50\n\n"
            "This means:\n"
            "• $5 per day target\n"
            "• $50 total for the 10-day segment"
        )
        return
    
    try:
        budget_per_day = Decimal(context.args[0])
        base_amount = Decimal(context.args[1])
        
        if budget_per_day <= 0 or base_amount <= 0:
            raise ValueError("Amounts must be positive")
        
        DatabaseManager.set_budget(db_user['id'], budget_per_day, base_amount)
        
        await update.message.reply_text(
            f"✅ Budget set successfully!\n\n"
            f"🎯 Daily Target: ${budget_per_day:.2f}\n"
            f"💰 10-Day Segment Total: ${base_amount:.2f}\n\n"
            f"You have ${base_amount:.2f} to spend over the next 10-day period."
        )
    except (ValueError, IndexError) as e:
        await update.message.reply_text(
            "❌ Invalid amounts. Please use numbers only.\n"
            "Example: /setbudget 5 50"
        )


async def add_expense_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /add command"""
    user = update.effective_user
    db_user = DatabaseManager.get_or_create_user(user.id, user.username, user.first_name)
    
    if len(context.args) < 1:
        await update.message.reply_text(
            "❌ Usage: /add <amount> [description]\n"
            "Example: /add 25.50 Lunch at restaurant"
        )
        return
    
    try:
        amount = Decimal(context.args[0])
        description = ' '.join(context.args[1:]) if len(context.args) > 1 else ""
        
        today = get_current_date()
        DatabaseManager.add_expense(db_user['id'], amount, today, description)
        
        daily = BudgetCalculator.calculate_daily_summary(db_user['id'])
        
        message = f"✅ Expense added: ${amount:.2f}"
        if description:
            message += f"\n📝 {description}"
        
        if daily:
            if daily['remaining_today'] >= 0:
                message += f"\n\n💰 Remaining today: ${daily['remaining_today']:.2f}"
                if daily['remaining_today'] < daily['current_daily_budget'] * Decimal('0.2'):
                    message += "\n⚠️ Getting close to your daily limit!"
            else:
                message += f"\n\n🚨 OVER BUDGET by ${abs(daily['remaining_today']):.2f}!"
                message += f"\n💰 Daily Limit: ${daily['current_daily_budget']:.2f}"
        
        await update.message.reply_text(message)
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid amount. Please use a number.\n"
            "Example: /add 25.50 Lunch"
        )


async def today_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /today command"""
    user = update.effective_user
    db_user = DatabaseManager.get_or_create_user(user.id, user.username, user.first_name)
    
    daily = BudgetCalculator.calculate_daily_summary(db_user['id'])
    
    if not daily:
        await update.message.reply_text(
            "❌ Please set your budget first using /setbudget"
        )
        return
    
    status = "✅" if daily['remaining_today'] >= 0 else "🚨"
    
    message = (
        f"📊 Daily Summary - {daily['date'].strftime('%Y-%m-%d')}\n\n"
        f"💵 Today's Budget: ${daily['current_daily_budget']:.2f}\n"
        f"💸 Spent Today: ${daily['spent_today']:.2f}\n"
        f"{status} Remaining: ${daily['remaining_today']:.2f}"
    )
    
    if daily['remaining_today'] < 0:
        message += f"\n\n🚨 OVER BUDGET by ${abs(daily['remaining_today']):.2f}!"
    elif daily['remaining_today'] < daily['current_daily_budget'] * Decimal('0.2'):
        message += "\n\n⚠️ Less than 20% remaining today!"
    
    await update.message.reply_text(message)




async def segment_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /segment command"""
    user = update.effective_user
    db_user = DatabaseManager.get_or_create_user(user.id, user.username, user.first_name)
    
    # Check if we're in a new segment
    is_new_segment = DatabaseManager.check_segment_reset(db_user['id'])
    
    segment = BudgetCalculator.calculate_segment_summary(db_user['id'])
    
    if not segment:
        await update.message.reply_text(
            "❌ Please set your budget first using /setbudget"
        )
        return
    
    # Get today's spending to show remaining for today
    daily = BudgetCalculator.calculate_daily_summary(db_user['id'])
    
    status = "✅" if segment['remaining_segment'] >= 0 else "⚠️"
    
    # Show new segment notification if applicable
    new_segment_msg = ""
    if is_new_segment and segment['days_passed'] <= 2:
        new_segment_msg = f"🎉 NEW SEGMENT STARTED! (Days {segment['start_date'].day}-{segment['end_date'].day})\n\n"
    
    message = (
        f"{new_segment_msg}"
        f"📈 Segment {segment['segment']} Summary (Days {segment['start_date'].day}-{segment['end_date'].day})\n\n"
        f"📅 Days Passed: {segment['days_passed']}/{segment['days_in_segment']}\n"
        f"⏳ Days Remaining: {segment['days_remaining']}\n\n"
        f"💰 Segment Budget: ${segment['segment_budget']:.2f}\n"
        f"💸 Spent So Far: ${segment['spent_segment']:.2f}\n"
        f"{status} Remaining: ${segment['remaining_segment']:.2f}\n\n"
    )
    
    # TODAY section
    message += f"📅 TODAY (Day {segment['days_passed']}):\n"
    if daily:
        message += f"💵 Budget: ${segment['current_daily_budget']:.2f}\n"
        message += f"💸 Spent: ${daily['spent_today']:.2f}\n"
        if daily['remaining_today'] > 0:
            message += f"✅ Left: ${daily['remaining_today']:.2f}"
        elif daily['remaining_today'] == 0:
            message += f"✅ Used all budget"
        else:
            message += f"🚨 Over by: ${abs(daily['remaining_today']):.2f}"
    
    # TOMORROW ONWARDS section
    if segment['days_remaining_from_tomorrow'] > 0:
        message += f"\n\n📅 TOMORROW ONWARDS ({segment['days_remaining_from_tomorrow']} days):\n"
        message += f"💡 Budget per day: ${segment['suggested_daily']:.2f}\n"
        
        if segment['suggested_daily'] < segment['target_daily']:
            # Calculate what you SHOULD have for tomorrow onwards
            expected_for_tomorrow = segment['target_daily'] * segment['days_remaining_from_tomorrow']
            
            # What you'll ACTUALLY have
            actual_remaining = segment['remaining_segment']
            
            # Deficit
            deficit = expected_for_tomorrow - actual_remaining
            
            # Amount reduced per day
            reduction_per_day = segment['target_daily'] - segment['suggested_daily']
            
            message += f"⚠️ ${reduction_per_day:.2f}/day less\n"
            if deficit > 0:
                message += f"💰 Need to save ${deficit:.2f} total to get back on track"
        elif segment['suggested_daily'] > segment['target_daily']:
            surplus = segment['suggested_daily'] - segment['target_daily']
            message += f"✅ ${surplus:.2f}/day extra!"
    
    # Warn if segment budget exceeded
    if segment['remaining_segment'] < 0:
        message += f"\n\n🚨 SEGMENT BUDGET EXCEEDED by ${abs(segment['remaining_segment']):.2f}!"
    
    await update.message.reply_text(message)


async def view_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /expenses command"""
    user = update.effective_user
    db_user = DatabaseManager.get_or_create_user(user.id, user.username, user.first_name)
    
    today = get_current_date()
    start_date = today - timedelta(days=7)
    
    expenses = DatabaseManager.get_expenses(db_user['id'], start_date, today)
    
    if not expenses:
        await update.message.reply_text("📋 No expenses in the last 7 days.")
        return
    
    message = "📋 Recent Expenses (Last 7 Days)\n\n"
    
    for exp in expenses[:10]:
        date_str = exp['expense_date'].strftime('%Y-%m-%d')
        desc = f" - {exp['description']}" if exp['description'] else ""
        message += f"• {date_str}: ${exp['amount']:.2f}{desc}\n"
    
    if len(expenses) > 10:
        message += f"\n... and {len(expenses) - 10} more"
    
    await update.message.reply_text(message)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses"""
    text = update.message.text
    
    if text == "💰 Add Expense":
        await update.message.reply_text("Send: /add <amount> [description]")
    elif text == "📊 Daily Summary":
        await today_summary(update, context)
    elif text == "📈 Segment Summary":
        await segment_summary(update, context)
    elif text == "⚙️ Set Budget":
        await update.message.reply_text("Send: /setbudget <daily> <base>")
    elif text == "📋 View Expenses":
        await view_expenses(update, context)


async def send_daily_notification(context: ContextTypes.DEFAULT_TYPE):
    """Send daily notifications to all users at 9 AM and adjust budgets"""
    conn = DatabaseManager.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users")
            users = cur.fetchall()
            
            today = get_current_date()
            yesterday = today - timedelta(days=1)
            
            for user in users:
                try:
                    # Get budget info
                    budget = DatabaseManager.get_budget(user['id'])
                    if not budget:
                        continue
                    
                    # Calculate what the adjusted daily should be for today
                    segment = BudgetCalculator.calculate_segment_summary(user['id'])
                    if not segment:
                        continue
                    
                    # Get yesterday's spending
                    spent_yesterday = DatabaseManager.get_total_spent(user['id'], yesterday, yesterday)
                    
                    # Determine today's budget based on yesterday's performance
                    yesterday_budget = budget.get('adjusted_daily') or budget['budget_per_day']
                    
                    # If overspent yesterday or underspent yesterday, adjust for today
                    days_remaining = segment['days_remaining']
                    if days_remaining > 0:
                        # Calculate new daily budget based on remaining segment budget
                        new_daily_budget = segment['remaining_segment'] / days_remaining
                        
                        # Update the adjusted daily for today
                        DatabaseManager.update_adjusted_daily(user['id'], new_daily_budget, today)
                        
                        segment_info = BudgetCalculator.get_segment_info(today)
                        
                        # Check if it's the first day of a new segment
                        is_segment_start = today.day in [1, 11, 21]
                        
                        if is_segment_start:
                            message = (
                                f"🎉 NEW SEGMENT STARTED!\n\n"
                                f"📅 Segment {segment_info['segment']} (Days {segment_info['start_date'].day}-{segment_info['end_date'].day})\n"
                                f"💰 Today's Budget: ${new_daily_budget:.2f}\n"
                                f"📆 Date: {today.strftime('%Y-%m-%d')}\n\n"
                                f"💡 Remember to set your budget with /setbudget if needed!"
                            )
                        else:
                            # Show budget adjustment info
                            if new_daily_budget < Decimal(budget['budget_per_day']):
                                message = (
                                    f"🌅 Good morning!\n\n"
                                    f"💰 Today's Budget: ${new_daily_budget:.2f}\n"
                                    f"⚠️ Adjusted down from ${budget['budget_per_day']:.2f} due to overspending\n"
                                    f"📅 Date: {today.strftime('%Y-%m-%d')}"
                                )
                            elif new_daily_budget > Decimal(budget['budget_per_day']):
                                message = (
                                    f"🌅 Good morning!\n\n"
                                    f"💰 Today's Budget: ${new_daily_budget:.2f}\n"
                                    f"✅ Adjusted up from ${budget['budget_per_day']:.2f} - you're doing great!\n"
                                    f"📅 Date: {today.strftime('%Y-%m-%d')}"
                                )
                            else:
                                message = (
                                    f"🌅 Good morning!\n\n"
                                    f"💰 Today's Budget: ${new_daily_budget:.2f}\n"
                                    f"📅 Date: {today.strftime('%Y-%m-%d')}"
                                )
                        
                        await context.bot.send_message(
                            chat_id=user['telegram_id'],
                            text=message
                        )
                except Exception as e:
                    logger.error(f"Error sending notification to user {user['id']}: {e}")
    finally:
        DatabaseManager.release_connection(conn)


def run_flask():
    """Run Flask server in a separate thread"""
    port = int(os.getenv('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)



def main():
    """Start the bot"""
    # Initialize database
    DatabaseManager.init_database()
    
    # Start Flask server in a separate thread (for Render health checks)
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask health check server started on port {os.getenv('PORT', 10000)}")
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setbudget", set_budget_command))
    application.add_handler(CommandHandler("add", add_expense_command))
    application.add_handler(CommandHandler("today", today_summary))
    application.add_handler(CommandHandler("segment", segment_summary))
    application.add_handler(CommandHandler("expenses", view_expenses))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Schedule daily notifications (9 AM GMT+7 every day)
    job_queue = application.job_queue
    # Create timezone-aware time object
    notification_time = datetime.strptime("09:00", "%H:%M").time().replace(tzinfo=TIMEZONE)
    job_queue.run_daily(
        send_daily_notification,
        time=notification_time
    )
    
    # Start bot
    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)



if __name__ == '__main__':
    main()