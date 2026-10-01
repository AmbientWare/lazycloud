-- The first characters of each token, kept so a person can tell their tokens
-- apart in a list. Tokens issued before this have none.
alter table api_tokens add column prefix text not null default '';
