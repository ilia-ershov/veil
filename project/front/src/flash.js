import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

// A one-time message handed over in the navigation state: navigate('/login', { state: { message } }).
// It is read once and then removed from the history entry, so a reload does not show it again.
export function useFlashMessage(key = 'message') {
  const location = useLocation()
  const navigate = useNavigate()
  const [message] = useState(() => location.state?.[key] || '')
  const pending = location.state?.[key] !== undefined

  useEffect(() => {
    if (!pending) return
    const { [key]: _, ...rest } = location.state // keep the rest, e.g. `from` for the login redirect
    const url = `${location.pathname}${location.search}${location.hash}`
    navigate(url, { replace: true, state: Object.keys(rest).length ? rest : null })
  }, [pending, key, location, navigate])

  return message
}
