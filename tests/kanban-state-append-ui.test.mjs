import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const bundlePath = path.join(repoRoot, 'plugins', 'kanban', 'dashboard', 'dist', 'index.js')

function loadStateEventForm(fetchJSON) {
  let registeredPage = null
  const state = []
  let cursor = 0
  const hooks = {
    useState(initial) {
      const index = cursor++
      if (!(index in state)) state[index] = typeof initial === 'function' ? initial() : initial
      const setValue = value => {
        state[index] = typeof value === 'function' ? value(state[index]) : value
      }
      return [state[index], setValue]
    },
    useEffect() {},
    useCallback(fn) { return fn },
    useMemo(fn) { return fn() },
    useRef(initial) { return { current: initial } },
  }
  const createElement = (type, props, ...children) => ({
    type,
    props: props || {},
    children: children.flat(Infinity).filter(child => child !== null && child !== false),
  })
  const components = Object.fromEntries(
    ['Card', 'CardContent', 'Badge', 'Button', 'Input', 'Label', 'Select', 'SelectOption']
      .map(name => [name, name]),
  )
  const window = {
    __HERMES_PLUGIN_SDK__: {
      React: { createElement, Component: class Component {} },
      components,
      hooks,
      utils: { cn: (...parts) => parts.filter(Boolean).join(' '), timeAgo: () => '' },
      fetchJSON,
    },
    __HERMES_PLUGINS__: {
      register(_name, page) { registeredPage = page },
    },
  }
  vm.runInNewContext(fs.readFileSync(bundlePath, 'utf8'), {
    window,
    console,
    URLSearchParams,
    setTimeout,
    clearTimeout,
  })
  assert.ok(registeredPage, 'kanban page registered')
  assert.equal(typeof registeredPage.StateEventAppendForm, 'function')
  return {
    render(props) {
      cursor = 0
      return registeredPage.StateEventAppendForm(props)
    },
  }
}

function walk(node, visit) {
  if (!node || typeof node !== 'object') return
  visit(node)
  for (const child of node.children || []) walk(child, visit)
}

function named(root, name) {
  let match = null
  walk(root, node => {
    if (node.props && node.props.name === name) match = node
  })
  assert.ok(match, `control ${name} exists`)
  return match
}

function textContent(root) {
  const values = []
  walk(root, node => {
    for (const child of node.children || []) {
      if (typeof child === 'string') values.push(child)
    }
  })
  return values.join(' ')
}

test('authorized dashboard form appends one independent rung and refreshes readback', async () => {
  const calls = []
  let refreshes = 0
  const harness = loadStateEventForm(async (url, options) => {
    calls.push({ url, options })
    return { inserted: 1, state_events: [{ state: 'deployed', value: true }] }
  })
  const props = {
    taskId: 't_subject',
    boardSlug: 'isolated-board',
    onAppended() { refreshes += 1 },
  }
  let tree = harness.render(props)
  const values = {
    state: 'deployed',
    value: 'true',
    issuer_task_id: 't_evidence',
    issuer_run_id: '42',
    issuer_profile: 'reviewer',
    receipt_id: 'deploy-receipt',
    manifest_id: 'deploy-manifest',
    occurred_at: '1787599000',
  }
  for (const [name, value] of Object.entries(values)) {
    named(tree, name).props.onChange({ target: { value } })
    tree = harness.render(props)
  }
  await tree.props.onSubmit({ preventDefault() {} })

  assert.equal(calls.length, 1)
  assert.match(calls[0].url, /\/tasks\/t_subject\/state-events/)
  assert.match(calls[0].url, /board=isolated-board/)
  const body = JSON.parse(calls[0].options.body)
  assert.deepEqual(body, {
    issuer_run_id: 42,
    issuer_task_id: 't_evidence',
    issuer_profile: 'reviewer',
    state_events: [{
      state: 'deployed',
      value: true,
      occurred_at: 1787599000,
      receipt_id: 'deploy-receipt',
      issued_by_run: 42,
      manifest_id: 'deploy-manifest',
    }],
  })
  assert.equal(refreshes, 1)
  assert.equal('released' in body, false)
  assert.equal('merged' in body, false)
  assert.equal('reviewed' in body, false)
})

test('dashboard form surfaces a rejected unauthorized issuer without refreshing', async () => {
  let refreshes = 0
  const harness = loadStateEventForm(async () => {
    throw new Error('400: {"detail":"issuer run is not authorized for the subject task"}')
  })
  const props = {
    taskId: 't_subject',
    boardSlug: 'isolated-board',
    onAppended() { refreshes += 1 },
  }
  let tree = harness.render(props)
  for (const [name, value] of Object.entries({
    state: 'reviewed',
    value: 'true',
    issuer_task_id: 't_unrelated',
    issuer_run_id: '77',
    issuer_profile: 'reviewer',
    receipt_id: 'bad-receipt',
    manifest_id: 'bad-manifest',
    occurred_at: '1787599001',
  })) {
    named(tree, name).props.onChange({ target: { value } })
    tree = harness.render(props)
  }
  await tree.props.onSubmit({ preventDefault() {} })
  tree = harness.render(props)

  assert.equal(refreshes, 0)
  assert.match(textContent(tree), /issuer run is not authorized/)
})
