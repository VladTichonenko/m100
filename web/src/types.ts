export type Position = {
  id: string;
  name: string;
  section: string;
  unit: string;
  planned: string;
  completed: string;
  unit_pay: string | null;
  planned_pay: string | null;
  completed_pay: string | null;
};

export type Line = {
  position_id: string | null;
  quantity: string | null;
  phrase: string;
  unit?: string | null;
  question?: string;
  candidates?: string[];
  details?: CardDetails;
  deleted?: boolean;
};

export type CardDetails = {
  name?: string | null;
  unit?: string | null;
  planned?: string | null;
  unit_pay?: string | null;
  planned_pay?: string | null;
  remaining?: string | null;
  completed_pay?: string | null;
};

export type Report = {
  draft: {
    id: string;
    day: string;
    mode: 'add' | 'replace';
    status: 'draft' | 'saving' | 'done' | 'cancelled';
    revision: number;
    lines: Line[];
    transcript: string;
    before: Record<string, string>;
  };
  catalog: Position[];
  table_url: string;
  bitrix_enabled: boolean;
  bot_url?: string;
  edit_url?: string;
  rewrite_requested?: boolean;
  notification_pending?: boolean;
  timezone?: string;
  bitrix_url?: string;
  today?: TodaySummary;
  profile?: { name: string; username: string | null; url: string | null; has_photo: boolean } | null;
  operation: {
    sheets_done: boolean;
    bitrix_done: boolean;
    bitrix_skipped: boolean;
  } | null;
};

export type TodaySummary = {
  day: string;
  lines: { position_id: string; name: string; unit: string; quantity: string; pay: string | null }[];
  total_pay: string | null;
};
