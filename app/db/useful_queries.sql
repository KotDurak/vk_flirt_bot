##1. Топ-10 самых активных пользователей
SELECT u.vk_user_id, COUNT(m.id) as msg_count
FROM users u
JOIN messages m ON u.id = m.user_id
GROUP BY u.id
ORDER BY msg_count DESC
LIMIT 10;


#2. Конверсия в оплату (Сколько людей купило хотя бы раз?)
SELECT
    COUNT(DISTINCT user_id) as total_users,
    COUNT(DISTINCT CASE WHEN status = 'paid' THEN user_id END) as paying_users,
    ROUND(COUNT(DISTINCT CASE WHEN status = 'paid' THEN user_id END) * 100.0 / COUNT(DISTINCT user_id), 2) as conversion_rate
FROM users
LEFT JOIN payments p ON users.id = p.user_id;

#3. Самые популярные персонажи (Кого любят больше всего?)

SELECT c.name, COUNT(m.id) as interactions
FROM characters c
JOIN messages m ON c.id = m.character_id
GROUP BY c.id
ORDER BY interactions DESC;

#4. Эффективность рефералки (Сколько реально принесла?)
SELECT
    COUNT(*) as total_referrals,
    SUM(CASE WHEN referral_bonus_claimed = 1 THEN 1 ELSE 0 END) as bonuses_paid
FROM users
WHERE referred_by IS NOT NULL;

#5. Средний баланс «живых» пользователей
SELECT AVG(messages) as avg_balance
FROM users
WHERE messages > 0 AND id IN (SELECT DISTINCT user_id FROM messages);