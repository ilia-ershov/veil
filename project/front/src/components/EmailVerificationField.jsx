import { useEffect, useId, useState } from 'react'
import { authApi } from '../api'
import { errorMessage } from '../messages'
import { isEmail } from '../validation'
import Notice from './Notice'

const RESEND_SECONDS = 60

// [seconds left, start(seconds)]. Starting also resets "now", so the first value shown is right
// even when the page has been open for a while.
function useCountdown() {
  const [{ deadline, now }, setClock] = useState({ deadline: 0, now: 0 })
  useEffect(() => {
    if (!deadline) return undefined
    const timer = setInterval(() => {
      const current = Date.now()
      setClock((clock) => ({ ...clock, now: current }))
      if (current >= deadline) clearInterval(timer)
    }, 500)
    return () => clearInterval(timer)
  }, [deadline])
  const start = (seconds) => {
    const current = Date.now()
    setClock({ deadline: current + seconds * 1000, now: current })
  }
  return [Math.max(0, Math.ceil((deadline - now) / 1000)), start]
}

/**
 * Email input with the "send code -> enter code" confirmation step.
 * The parent keeps `email` and the resulting verification `token`; changing the
 * email must clear the token (the server binds the token to one address).
 */
export default function EmailVerificationField({ label, purpose, email, onEmailChange, token, onVerified }) {
  const id = useId()
  const [sentTo, setSentTo] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [secondsLeft, startCountdown] = useCountdown()

  const normalized = email.trim().toLowerCase()
  const codeSent = sentTo !== '' && sentTo === normalized
  const verified = Boolean(token)

  const sendCode = async () => {
    setError('')
    if (!isEmail(email)) {
      setError('Введите действительный адрес электронной почты.')
      return
    }
    setBusy(true)
    try {
      await authApi.requestCode(normalized, purpose)
      setSentTo(normalized)
      setCode('')
      startCountdown(RESEND_SECONDS)
    } catch (err) {
      setError(errorMessage(err))
      const retryAfter = err.response?.data?.retry_after
      if (retryAfter) {
        setSentTo(normalized) // a code was already sent a moment ago
        startCountdown(retryAfter)
      }
    } finally {
      setBusy(false)
    }
  }

  const verify = async () => {
    setError('')
    if (!/^\d{6}$/.test(code.trim())) {
      setError('Код состоит из 6 цифр.')
      return
    }
    setBusy(true)
    try {
      const result = await authApi.verifyCode(normalized, code.trim(), purpose)
      onVerified(result.verification_token)
    } catch (err) {
      setError(errorMessage(err))
      if (err.response?.data?.code === 'too_many_attempts') setSentTo('')
    } finally {
      setBusy(false)
    }
  }

  const reset = () => {
    onVerified('')
    setSentTo('')
    setCode('')
  }

  // Enter inside these inputs must not submit the surrounding form.
  const onEnter = (action) => (e) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      action()
    }
  }

  return (
    <div className="field">
      <label htmlFor={`${id}-email`}>{label}</label>
      <div className="input-row">
        <input
          id={`${id}-email`}
          type="email"
          placeholder="name@example.com"
          autoComplete="email"
          value={email}
          readOnly={verified}
          onChange={(e) => onEmailChange(e.target.value)}
          onKeyDown={onEnter(sendCode)}
          required
        />
        {verified ? (
          <button type="button" className="button button-ghost" onClick={reset}>
            Изменить
          </button>
        ) : (
          <button type="button" className="button" onClick={sendCode} disabled={busy || (codeSent && secondsLeft > 0)}>
            {codeSent ? (secondsLeft > 0 ? `Повторно через ${secondsLeft} с` : 'Отправить ещё раз') : 'Получить код'}
          </button>
        )}
      </div>

      {verified && <Notice type="success">Адрес подтверждён.</Notice>}

      {codeSent && !verified && (
        <div className="subfield">
          <p className="hint">
            Мы отправили 6-значный код на {normalized}. Он действует 10 минут. Не видите письмо — проверьте папку «Спам».
          </p>
          <label htmlFor={`${id}-code`}>Код из письма</label>
          <div className="input-row">
            <input
              id={`${id}-code`}
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              placeholder="123456"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
              onKeyDown={onEnter(verify)}
            />
            <button type="button" className="button button-primary" onClick={verify} disabled={busy}>
              Подтвердить
            </button>
          </div>
        </div>
      )}

      <Notice>{error}</Notice>
    </div>
  )
}
