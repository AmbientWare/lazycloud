"""Atomic map reads and conditional writes share value-and-expiry revisions."""

READ = """
local value = redis.call('GET', KEYS[1])
if not value then return {} end
local expiry = redis.call('PEXPIRETIME', KEYS[1])
return {value, redis.sha1hex(value) .. ':' .. expiry, expiry}
"""

WRITE = """
local current = redis.call('GET', KEYS[1])
if ARGV[5] == 'absent' and current then return 0 end
if ARGV[5] == 'revision' then
    if not current then return 0 end
    local expiry = redis.call('PEXPIRETIME', KEYS[1])
    if redis.sha1hex(current) .. ':' .. expiry ~= ARGV[6] then return 0 end
end
if ARGV[4] == 'keep' then
    if not current then return 0 end
    redis.call('SET', KEYS[1], ARGV[3], 'KEEPTTL')
elseif ARGV[4] == '0' then
    redis.call('SET', KEYS[1], ARGV[3])
else
    redis.call('SET', KEYS[1], ARGV[3], 'EX', ARGV[4])
end
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('SADD', KEYS[3], ARGV[2])
return 1
"""

DELETE = """
if ARGV[2] ~= '' then
    local current = redis.call('GET', KEYS[1])
    if not current then return 0 end
    local expiry = redis.call('PEXPIRETIME', KEYS[1])
    if redis.sha1hex(current) .. ':' .. expiry ~= ARGV[2] then return 0 end
end
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[2], ARGV[1])
return 1
"""

KEY_PAGE = """
local page = redis.call('SSCAN', KEYS[1], ARGV[1], 'COUNT', ARGV[2])
local result = {page[1]}
for _, key in ipairs(page[2]) do
    local entry = string.gsub(ARGV[3] .. key, ':+$', '')
    if redis.call('EXISTS', entry) == 0 then
        redis.call('SREM', KEYS[1], key)
    elseif string.sub(key, 1, string.len(ARGV[4])) == ARGV[4] then
        table.insert(result, key)
    end
end
return result
"""

DELETE_MAP = """
local keys = redis.call('SMEMBERS', KEYS[1])
for _, key in ipairs(keys) do
    local entry = string.gsub(ARGV[1] .. key, ':+$', '')
    redis.call('DEL', entry)
end
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[2], ARGV[2])
return #keys
"""
