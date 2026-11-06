import { useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { authApi } from '../api'
import { setToken } from '../auth'
import Notice from '../components/Notice'
import PasswordField from '../components/PasswordField'
import { useFlashMessage } from '../flash'
import { errorMessage } from '../messages'

export default function Login() {
  const navigate = useNavigate()
  const location = useLocation()
  const [params] = useSearchParams()
  const flash = useFlashMessage()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [mfaToken, setMfaToken] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const info = flash || (params.get('expired') ? 'Сеанс завершён. Войдите снова.' : '')

  const finish = (token) => {
    setToken(token)
    navigate(location.state?.from || '/dashboard', { replace: true })
  }

  const submitPassword = async (e) => {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      const result = await authApi.login(email.trim(), password)
      if (result.mfa_required) {
        setMfaToken(result.mfa_token)
        setPassword('')
      } else {
        finish(result.token)
      }
    } catch (err) {
      setError(errorMessage(err, 'Не удалось войти.'))
    } finally {
      setBusy(false)
    }
  }

  const submitCode = async (e) => {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      const result = await authApi.loginSecondFactor(mfaToken, code.trim())
      finish(result.token)
    } catch (err) {
      setError(errorMessage(err, 'Не удалось войти.'))
      if (err.response?.data?.code === 'mfa_expired') {
        setMfaToken('')
        setCode('')
      }
    } finally {
      setBusy(false)
    }
  }

  if (mfaToken) {
    return (
      <section className="card card-narrow">
        <h1>Подтверждение входа</h1>
        <p className="muted">Введите 6-значный код из приложения-аутентификатора или один из кодов восстановления.</p>
        <form onSubmit={submitCode} className="form">
          <div className="field">
            <label htmlFor="mfa-code">Код</label>
            <input
              id="mfa-code"
              autoComplete="one-time-code"
              autoFocus
              value={code}
              maxLength={20}
              onChange={(e) => setCode(e.target.value)}
              required
            />
          </div>
          <Notice>{error}</Notice>
          <div className="actions">
            <button type="submit" className="button button-primary" disabled={busy}>
              Войти
            </button>
            <button type="button" className="button button-ghost" onClick={() => setMfaToken('')}>
              Назад
            </button>
          </div>
        </form>
      </section>
    )
  }

  return (
    <section className="card card-narrow">
      <h1>Вход</h1>
      <Notice type="info">{info}</Notice>
      <form onSubmit={submitPassword} className="form">
        <div className="field">
          <label htmlFor="email">Основной адрес</label>
          <input
            id="email"
            type="email"
            autoComplete="username"
            placeholder="name@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </div>
        <PasswordField label="Пароль" value={password} onChange={setPassword} autoComplete="current-password" />
        <Notice>{error}</Notice>
        <button type="submit" className="button button-primary button-wide" disabled={busy}>
          Войти
        </button>
      </form>
      <p className="form-links">
        <Link to="/reset-password">Забыли пароль?</Link>
        <Link to="/signup">Создать аккаунт</Link>
      </p>
    </section>
  )
}
