import { Navigate, Route, Routes } from "react-router";
import { AppShell } from "@/components/AppShell";
import { useAthleteContext } from "@/components/AthleteProvider";
import { CalendarPage } from "@/pages/CalendarPage";
import { ChatPage } from "@/pages/ChatPage";
import { DecisionsPage } from "@/pages/DecisionsPage";
import { LogPage } from "@/pages/LogPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { ProgressPage } from "@/pages/ProgressPage";
import { SetupPage } from "@/pages/SetupPage";

/**
 * `/` goes to the calendar when there is an ACTIVE block and to setup otherwise.
 * "Active" is the point: a draft block (wizard created, not yet approved) has no
 * sessions, and sending someone half-way through setup to an empty calendar would
 * strand them.
 */
export function HomeRedirect() {
  const { currentBlockId } = useAthleteContext();
  return <Navigate to={currentBlockId === null ? "/setup" : "/calendar"} replace />;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<HomeRedirect />} />
        <Route path="setup" element={<SetupPage />} />
        <Route path="calendar" element={<CalendarPage />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="log" element={<LogPage />} />
        <Route path="progress" element={<ProgressPage />} />
        <Route path="decisions" element={<DecisionsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
