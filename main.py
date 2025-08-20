import telebot

BOT_TOKEN = "8201866380:AAHqqHctF76jYAgV1Q90HsiEobfazfvmaD0"
bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "হ্যালো! 🖐 আমি তোমার বট, ঠিকভাবে কাজ করছি ✅")

@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, message.text)

print("Bot is running...")
bot.polling()
