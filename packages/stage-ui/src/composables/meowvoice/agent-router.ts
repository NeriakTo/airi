import { computed, ref } from 'vue'

import { useAiriCardStore } from '../../stores/modules/airi-card'

interface AgentRoute {
  cardId: string
  displayName: string
  prefixes: string[]
}

const AGENT_ROUTES: AgentRoute[] = [
  { cardId: 'cyanmeow', displayName: '青喵', prefixes: ['青喵', '嘿青喵', 'cyanmeow'] },
  { cardId: 'default', displayName: 'ReLU', prefixes: ['relu'] },
]

export function useMeowVoiceRouter() {
  const cardStore = useAiriCardStore()
  const lastSwitchedTo = ref<string | null>(null)

  const activeAgentName = computed(() => {
    const card = cardStore.activeCard
    return card?.name ?? 'Unknown'
  })

  function routeText(text: string): { routedText: string, switched: boolean } {
    const trimmed = text.trim()
    const lower = trimmed.toLowerCase()

    for (const route of AGENT_ROUTES) {
      for (const prefix of route.prefixes) {
        if (!lower.startsWith(prefix))
          continue

        const rest = trimmed.slice(prefix.length).trim()
        const alreadyActive = cardStore.activeCardId === route.cardId

        if (!alreadyActive && cardStore.cards.has(route.cardId)) {
          cardStore.activeCardId = route.cardId
          lastSwitchedTo.value = route.displayName
        }

        if (rest.length > 0)
          return { routedText: rest, switched: !alreadyActive }

        return { routedText: '', switched: !alreadyActive }
      }
    }

    return { routedText: trimmed, switched: false }
  }

  function addRoute(route: AgentRoute) {
    const existing = AGENT_ROUTES.findIndex(r => r.cardId === route.cardId)
    if (existing >= 0)
      AGENT_ROUTES[existing] = route
    else
      AGENT_ROUTES.push(route)
  }

  return {
    routeText,
    addRoute,
    activeAgentName,
    lastSwitchedTo,
  }
}
