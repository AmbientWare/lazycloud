from models.k8s import WorkloadType
from models.statuses import DeploymentStatus, ServiceStatus, ServiceStatusSummary
from textual.app import ComposeResult
from textual.reactive import reactive

from cli.api import api
from cli.ui.textual.components import Container, ListItemData, ListView
from cli.ui.textual.components.listview import ListItem
from cli.ui.textual.messages import ServiceSelected
from cli.ui.textual.theme import Icons


class ServicesContainer(Container):
    deployment_id: reactive[str | None] = reactive(None)
    deployment_status: reactive[DeploymentStatus | None] = reactive(None)
    services: reactive[list[ServiceStatus] | None] = reactive(None)
    selected_service: reactive[ServiceStatus | None] = reactive(None)

    BINDINGS = [
        ("up,k", "cursor_up", "Move up"),
        ("down,j", "cursor_down", "Move down"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._list_view = None
        self._service_status_cache: dict[str, ServiceStatus] = {}
        self._detailed_service_names: set[str] = set()
        self._loading_service_names: set[str] = set()
        self._content_loading_requests = 0
        self.border_title = f"{Icons.WRENCH} [2] Services"

    def compose(self) -> ComposeResult:
        """Create the services list"""
        self._list_view = ListView(
            on_select=lambda item_data: (
                self.call_later(self._handle_selection, item_data),
                None,
            )[-1],
            on_highlight=self._handle_highlight,
            id="services-list",
        )
        yield self._list_view

    def on_mount(self) -> None:
        """Style the container when mounted"""
        self.can_focus = True
        # set empty message after mount
        if self._list_view:
            self._list_view._empty_message = "Select a deployment to view services"

    async def on_focus(self) -> None:
        """Handle focus event."""
        self.border_subtitle = "↑↓/jk Navigate"

        # Ensure an item is highlighted in the list
        if self._list_view:
            self._list_view.ensure_highlighted()
            service_name = self._get_highlighted_service_name()
            if service_name and (
                not self.selected_service or self.selected_service.name != service_name
            ):
                self._schedule_set_selected_service(service_name)

    def on_blur(self) -> None:
        """Handle blur event."""
        self.border_subtitle = None

    def on_click(self) -> None:
        """Handle mouse clicks - switch to services view."""
        self.app.action_switch_to_services()

    async def watch_deployment_id(self, old_value, new_value) -> None:
        """Auto-refresh when services list changes"""
        if old_value != new_value:
            self._service_status_cache.clear()
            self._detailed_service_names.clear()
            self._loading_service_names.clear()
            self._set_content_loading(False, reset=True)
            self.selected_service = None

        if new_value is not None and self._list_view:
            if (
                self.deployment_status
                and self.deployment_status.deployment_id == new_value
            ):
                self._update_list_from_deployment_status(self.deployment_status)
            else:
                await self.refresh_services()
        elif self._list_view:
            self.clear_services()

    def watch_deployment_status(
        self, _old_value: DeploymentStatus | None, new_value: DeploymentStatus | None
    ) -> None:
        """Refresh service names from deployment status when available."""
        if (
            new_value
            and self._list_view
            and self.deployment_id
            and new_value.deployment_id == self.deployment_id
        ):
            self._update_list_from_deployment_status(new_value)

    async def watch_selected_service(self, _old_value, new_value) -> None:
        """React when a service is selected - post message for other components"""
        if new_value and self.deployment_id:
            # Post message instead of directly updating other containers
            self.post_message(
                ServiceSelected(
                    deployment_id=self.deployment_id,
                    service_status=new_value,
                )
            )

    async def refresh_services(self) -> None:
        """Update the services list for a selected deployment."""
        if not self._list_view or not self.deployment_id:
            return

        self._list_view.show_loading("Loading services...")
        try:
            service_statuses = await api.services.get_service_statuses(
                self.deployment_id
            )

            items = []
            services = []
            if service_statuses:
                for status in service_statuses:
                    self._service_status_cache[status.service.name] = status.service
                    services.append(status.service)
                    items.append(
                        ListItemData(
                            id=status.service.name,
                            name=status.service.name,
                            status=str(status.service.status),
                            extra_text=f"{status.service.ready_replicas}/{status.service.replicas}",
                            data=status.service.name,
                        )
                    )

            self._list_view.update_items(items)
            self.services = services

            # Prefetch detail payloads for the first couple services to reduce
            # perceived latency when entering the service details pane.
            for item in items[:2]:
                self.run_worker(
                    self._fetch_service_details(
                        service_name=item.id,
                        update_selected=False,
                        show_loading=False,
                    ),
                    exclusive=False,
                )

        except Exception as e:
            self.log.error(f"Failed to load services: {e}")
            self._list_view.update_items([])
            self._list_view.show_empty_message()

        finally:
            self._list_view.hide_loading()

    async def _handle_selection(self, item_data: ListItemData) -> None:
        """Handle service selection (Enter key pressed)."""
        service_name = self._get_service_name(item_data)
        if service_name:
            self._schedule_set_selected_service(service_name)

    def _handle_highlight(self, item_data: ListItemData) -> None:
        """Handle service highlight with api request debouncing."""
        self._selection_timer = self.handle_debounce(
            self._selection_timer,
            lambda: (
                self.call_later(self._update_selected_service, item_data),
                None,
            )[-1],
        )

    async def _update_selected_service(self, item_data: ListItemData) -> None:
        """Fetch service status after debounce delay."""
        self._selection_timer = None

        service_name = self._get_service_name(item_data)
        if service_name and (
            not self.selected_service or self.selected_service.name != service_name
        ):
            self._schedule_set_selected_service(service_name)

    def _update_list_from_deployment_status(
        self, deployment_status: DeploymentStatus
    ) -> None:
        """Update service list using already-fetched deployment status data."""
        if not self._list_view:
            return

        services: list[ServiceStatus] = []
        items: list[ListItemData] = []

        for summary in deployment_status.services:
            if summary.name not in self._detailed_service_names:
                self._service_status_cache[summary.name] = self._summary_to_service_status(
                    summary,
                    deployment_status,
                )

            cached = self._service_status_cache.get(summary.name)
            if cached:
                services.append(cached)
                items.append(
                    ListItemData(
                        id=summary.name,
                        name=summary.name,
                        status=str(summary.status),
                        extra_text=f"{summary.ready_replicas}/{summary.total_replicas}",
                        data=summary.name,
                    )
                )

        self._list_view.update_items(items)
        self.services = services

        if self.selected_service and self.selected_service.name in self._service_status_cache:
            selected_name = self.selected_service.name
            if selected_name not in self._detailed_service_names:
                self.selected_service = self._service_status_cache[selected_name]

    def _summary_to_service_status(
        self,
        summary: ServiceStatusSummary,
        deployment_status: DeploymentStatus,
    ) -> ServiceStatus:
        """Convert deployment summary service data into a full ServiceStatus shape."""
        return ServiceStatus(
            name=summary.name,
            image=summary.image or "unknown:latest",
            workload_type=summary.workload_type or WorkloadType.DEPLOYMENT,
            status=summary.status,
            replicas=summary.total_replicas,
            ready_replicas=summary.ready_replicas,
            pods=summary.pods,
            resources=summary.resources,
            current_usage=summary.current_usage,
            ports=summary.ports,
            volumes=None,
            hpa=summary.hpa,
            healthcheck=summary.healthcheck,
            total_restarts=summary.restarts,
            last_checked=deployment_status.last_checked,
            endpoint=summary.endpoint,
            custom_domain=summary.custom_domain,
            domain_status=summary.domain_status,
            cname_target=summary.cname_target,
        )

    def _get_service_name(self, item_data: ListItemData) -> str | None:
        """Extract service name from list item data."""
        if isinstance(item_data.data, ServiceStatus):
            return item_data.data.name
        if isinstance(item_data.data, str):
            return item_data.data
        return item_data.id or None

    def _get_highlighted_service_name(self) -> str | None:
        """Get service name for currently highlighted list item."""
        if (
            not self._list_view
            or self._list_view.index is None
            or self._list_view.index >= len(self._list_view.children)
        ):
            return None

        item = self._list_view.children[self._list_view.index]
        if not isinstance(item, ListItem):
            return None
        return self._get_service_name(item.item_data)

    async def _set_selected_service(self, service_name: str) -> None:
        """Set selected service from cache or fetch once from API."""
        cached = self._service_status_cache.get(service_name)
        if cached is not None:
            self.selected_service = cached

        if service_name in self._detailed_service_names:
            return

        await self._fetch_service_details(
            service_name=service_name,
            update_selected=True,
            show_loading=True,
        )

    async def _fetch_service_details(
        self,
        service_name: str,
        update_selected: bool,
        show_loading: bool,
    ) -> None:
        """Fetch full service details and cache the result."""
        deployment_id = self.deployment_id
        if not deployment_id or service_name in self._loading_service_names:
            return

        if show_loading:
            self._set_content_loading(True)

        self._loading_service_names.add(service_name)
        try:
            status = await api.services.get_service_status(
                deployment_id,
                service_name,
                fast=True,
            )
            if deployment_id != self.deployment_id:
                return

            self._service_status_cache[service_name] = status.service
            self._detailed_service_names.add(service_name)

            if update_selected:
                # Only overwrite if user hasn't moved to a different service.
                if not self.selected_service or self.selected_service.name == service_name:
                    self.selected_service = status.service
        except Exception as e:
            self.log.error(f"Failed to get service status for {service_name}: {e}")
        finally:
            self._loading_service_names.discard(service_name)
            if show_loading:
                self._set_content_loading(False)

    def _schedule_set_selected_service(self, service_name: str) -> None:
        """Schedule service selection work without blocking UI event handlers."""
        self.run_worker(self._set_selected_service(service_name), exclusive=False)

    def _set_content_loading(self, is_loading: bool, reset: bool = False) -> None:
        """Toggle loading state for details pane to avoid frozen feel."""
        if reset:
            self._content_loading_requests = 0
        elif is_loading:
            self._content_loading_requests += 1
        else:
            self._content_loading_requests = max(0, self._content_loading_requests - 1)

        try:
            content_container = self.app.query_one("#main-container")
            content_container.loading = self._content_loading_requests > 0
        except Exception:
            pass

    def clear_services(self) -> None:
        """Clear the services list."""
        if self._list_view:
            self._list_view.update_items([])
            self._list_view.show_empty_message()
        self._set_content_loading(False, reset=True)
        self.services = None

    def action_cursor_up(self) -> None:
        """Move cursor up in the list."""
        if self._list_view:
            self._list_view.action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move cursor down in the list."""
        if self._list_view:
            self._list_view.action_cursor_down()

    def action_select_item(self) -> None:
        """Select the currently highlighted item."""
        if self._list_view:
            self._list_view.action_select_cursor()
