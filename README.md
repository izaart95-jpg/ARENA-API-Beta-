# ARENA-API-Beta
Basic Arena API Currently in Beta Testing 
# Reminder 
the project is in very early stage of devlopment and currently im working on recapthca evasion so cmplete automation isnt possible as of now ill try to improve it 
if you have any recommendation 
you can contact me at izaart95@gmail.com

MOBILE IS SUPPORTED USE KIWI BROWSER OR ERUDA

# Usage
If you are using Lmarena without logging in keep v2_auth False in config.json 
Go to Lmarena website Start a Chat with any model copy url For instance url is https://arena.ai/c/eval_id Open Devtools Get auth-prod-v1 cookie From applicaions tab also get __cf_clearnece and __cf_bm  
Install Dependencies
Run python main.py
Enter Auth-prod-v1 and otheer cookies in eval_id paste the uuid from url enter model Id you can get ids frm models.json 
On first time if you dont have Recaptcha tokens just quit the script with ctl+c 
run python server.py
Opeb the url again in Any browser with devtools paste  harvestorv3.js content it will start generating captcha and send them to server 
re run main.py now it should work if you get recaptcha validation failed even when harvestorv3 is running you have tosolve captcha on browser website and rerun harvestor 


First Run harvest v3 on browser 
