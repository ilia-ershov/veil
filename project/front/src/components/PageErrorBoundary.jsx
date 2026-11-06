import { Component } from 'react'

// Without this a page that fails to render leaves a blank screen. The usual cause is a new version
// of the site: a tab opened before the update asks for page files that no longer exist.
export default class PageErrorBoundary extends Component {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <section className="card card-narrow" role="alert">
        <h1>Не удалось открыть страницу</h1>
        <p className="muted">Возможно, сайт только что обновился. Обновите страницу, чтобы загрузить новую версию.</p>
        <button type="button" className="button button-primary" onClick={() => window.location.reload()}>
          Обновить страницу
        </button>
      </section>
    )
  }
}
