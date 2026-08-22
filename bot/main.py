import os, asyncio, aiomysql
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters
from handlers import handle_incoming_text, handle_pipeline_routing, handle_photo_upload, trigger_manual_override, process_manual_replies

def main():
    app = Application.builder().token(os.getenv('TELEGRAM_BOT_TOKEN')).build()
    loop = asyncio.get_event_loop()
    
    db_config = {
        'host': os.getenv('DB_HOST', 'mysql_db'),
        'user': os.getenv('DB_USER'),
        'password': os.getenv('DB_PASSWORD'),
        'db': os.getenv('DB_NAME'),
        'autocommit': True
    }
    app.bot_data['db_pool'] = loop.run_until_complete(aiomysql.create_pool(**db_config))

    app.add_handler(CallbackQueryHandler(handle_pipeline_routing, pattern="^(dep_|arr_)"))
    app.add_handler(CallbackQueryHandler(trigger_manual_override, pattern="^(fixdep_|fixarr_|manbol_)"))
    
    # Standard capitalized REPLY configuration filters
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, process_manual_replies))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_upload))
    
    app.run_polling()

if __name__ == '__main__':
    main()
