import { Badge, Box, Flex, HStack, Menu, Portal, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { NavLink, Outlet } from "react-router";
import { useAthlete } from "@/api/hooks/athletes";
import { useBlockState } from "@/api/hooks/blocks";
import { useProposals } from "@/api/hooks/proposals";
import { blockLabel } from "@/lib/blockLabel";
import { useAthleteContext } from "./AthleteProvider";
import { SyntheticBanner } from "./SyntheticBanner";
import { ColorModeButton } from "./ui/color-mode";

interface NavItem {
  to: string;
  label: string;
}

const CALENDAR: NavItem = { to: "/calendar", label: "Calendar" };
const CHAT: NavItem = { to: "/chat", label: "Chat" };
const LOG: NavItem = { to: "/log", label: "Log" };
const PROGRESS: NavItem = { to: "/progress", label: "Progress" };
const DECISIONS: NavItem = { to: "/decisions", label: "Decisions" };
const SETUP: NavItem = { to: "/setup", label: "Setup" };

const SIDEBAR = [CALENDAR, CHAT, LOG, PROGRESS, DECISIONS, SETUP];
const TABS = [CALENDAR, LOG, CHAT];
const MORE = [PROGRESS, DECISIONS, SETUP];

export function AppShell() {
  const { athlete, athleteId, currentBlock, currentBlockId } = useAthleteContext();
  const detail = useAthlete(athleteId);
  const state = useBlockState(currentBlockId);
  const pending = useProposals("pending", athleteId);
  const pendingCount = pending.data?.length ?? 0;

  return (
    // minW="0" on every flex child: without it a wide table or long word pushes the
    // page past 375 px and the whole app scrolls sideways.
    <Flex minH="100vh" direction="column">
      {detail.data?.has_synthetic_one_rms && <SyntheticBanner />}

      <Flex flex="1" minW="0">
        <Box
          as="nav"
          aria-label="Main"
          display={{ base: "none", md: "block" }}
          w="56"
          flexShrink="0"
          borderEndWidth="1px"
          p="3"
        >
          {SIDEBAR.map((item) => (
            <NavItemLink key={item.to} item={item} badge={item === CHAT ? pendingCount : 0} />
          ))}
        </Box>

        <Flex flex="1" direction="column" minW="0">
          <HStack as="header" px="4" py="3" borderBottomWidth="1px" justify="space-between" minW="0">
            <Box minW="0">
              <Text fontWeight="semibold" truncate>
                {athlete?.name ?? "Powerlifting Block Planner"}
              </Text>
              <Text fontSize="sm" color="fg.muted" truncate>
                {blockLabel(currentBlock, state.data, state.isError)}
              </Text>
            </Box>
            <ColorModeButton />
          </HStack>

          {/* Bottom padding clears the fixed tab bar on a phone. */}
          <Box as="main" flex="1" minW="0" p="4" pb={{ base: "20", md: "4" }}>
            <Outlet />
          </Box>
        </Flex>
      </Flex>

      <MobileTabs pendingCount={pendingCount} />
    </Flex>
  );
}

function NavItemLink({ item, badge }: { item: NavItem; badge: number }) {
  return (
    <NavLink to={item.to} style={{ textDecoration: "none" }}>
      {({ isActive }) => (
        <Flex
          align="center"
          justify="space-between"
          px="3"
          py="2"
          rounded="md"
          bg={isActive ? "bg.muted" : undefined}
          fontWeight={isActive ? "semibold" : "normal"}
          _hover={{ bg: "bg.subtle" }}
        >
          {item.label}
          <PendingBadge count={badge} />
        </Flex>
      )}
    </NavLink>
  );
}

function PendingBadge({ count }: { count: number }): ReactNode {
  if (count === 0) return null;
  return (
    <Badge colorPalette="orange" variant="solid" aria-label={`${count} pending proposals`}>
      {count}
    </Badge>
  );
}

function MobileTabs({ pendingCount }: { pendingCount: number }) {
  return (
    <Flex
      as="nav"
      aria-label="Main"
      display={{ base: "flex", md: "none" }}
      position="fixed"
      bottom="0"
      insetX="0"
      bg="bg"
      borderTopWidth="1px"
      zIndex="sticky"
    >
      {TABS.map((item) => (
        <NavLink key={item.to} to={item.to} style={{ flex: 1, textDecoration: "none" }}>
          {({ isActive }) => (
            <Flex
              direction="column"
              align="center"
              py="3"
              gap="1"
              fontSize="sm"
              fontWeight={isActive ? "semibold" : "normal"}
              color={isActive ? "fg" : "fg.muted"}
            >
              <HStack gap="1">
                {item.label}
                {item === CHAT && <PendingBadge count={pendingCount} />}
              </HStack>
            </Flex>
          )}
        </NavLink>
      ))}

      <Menu.Root positioning={{ placement: "top-end" }}>
        <Menu.Trigger asChild>
          <Flex flex="1" py="3" justify="center" fontSize="sm" color="fg.muted" cursor="pointer" as="button">
            More
          </Flex>
        </Menu.Trigger>
        <Portal>
          <Menu.Positioner>
            <Menu.Content>
              {MORE.map((item) => (
                <Menu.Item key={item.to} value={item.to} asChild>
                  <NavLink to={item.to}>{item.label}</NavLink>
                </Menu.Item>
              ))}
            </Menu.Content>
          </Menu.Positioner>
        </Portal>
      </Menu.Root>
    </Flex>
  );
}
