import { Search } from "lucide-react";
import { Input } from "@/components/ui/input";

export function SearchBar() {
  return (
    <div className="relative w-full sm:w-64">
      <Search className="text-muted-foreground absolute top-2.5 left-2 h-4 w-4" />
      <Input type="search" placeholder="Search machines" className="pl-8" />
    </div>
  );
}
