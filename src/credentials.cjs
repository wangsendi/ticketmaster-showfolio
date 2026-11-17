'use strict';

// HTTP 200 can still contain a rejected-credentials business response.
function credentialsRejected(body) {
  if (!body || typeof body.message !== 'string') return false;
  return /\bcredentials?\s+mismatch\b|\binvalid\s+credentials\b|\bincorrect\s+(?:email\s+(?:or|and)\s+)?password\b|账号或密码错误|邮箱或密码错误|密码不正确/i.test(body.message);
}

module.exports = { credentialsRejected };
