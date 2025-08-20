import telebot

BOT_TOKEN = "8201866380:AAHqqHctF76jYAgV1Q90HsiEobfazfvmaD0"  
bot = telebot.TeleBot(BOT_TOKEN)

# /start কমান্ড
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "✅ বট কাজ করছে")

# /hello কমান্ড
@bot.message_handler(commands=['hello'])
def hello(message):
    bot.reply_to(message, "😘🥰")

print("Bot is running...")
bot.infinity
