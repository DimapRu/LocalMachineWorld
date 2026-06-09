# Архитектура lmWorld

## Обзор системы

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FRONTEND (Next.js)                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐ │
│  │    Office    │  │    Hire      │  │    Agent     │  │     Kanban        │ │
│  │   (2D Grid)  │  │   Manager    │  │   Profile    │  │     Board        │ │
│  │ Agent cards  │  │ Add new agent│  │ Model+MCP    │  │   Tasks flow     │ │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────────┘ │
│                              ↕ WebSocket / REST API                           │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │
┌────────────────────────────────────┴────────────────────────────────────────┐
│                            BACKEND (FastAPI)                                 │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                         API LAYER                                       │   │
│  │  POST /agents          - создать агента                                   │   │
│  │  GET  /agents         - список агентов                                  │   │
│  │  GET  /agents/{id}    - профиль агента                                   │   │
│  │  PUT  /agents/{id}    - обновить профиль                                  │   │
│  │  DELETE /agents/{id}  - уволить агента                                   │   │
│  │  POST /agents/{id}/hire - принять на работу                            │   │
│  │  POST /agents/{id}/fire - уволить                                        │   │
│  │  PUT  /agents/{id}/director - назначить директором                       │   │
│  │                                                                       │   │
│  │  GET  /models         - каталог моделей (HuggingFace)                    │   │
│  │  GET  /mcp-tools      - доступные инструменты                           │   │
│  │                                                                       │   │
│  │  WebSocket /ws/agent/{id} - real-time логи агента                          │   │
│  │  WebSocket /ws/office - вся команда в реальном времени                   │   │
│  └────────────────────────────────────────────────���─────────────────────┘   │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                        AGENT FACTORY                                    │   │
│  │  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐      │   │
│  │  │ ProfileBuilder │  │    RoleManager  │  │   SkillManager  │      │   │
│  │  │ - name         │  │ - is_director   │  │ - add_mcp_tool │      │   │
│  │  │ - model        │  │ - set_primary   │  │ - remove_tool │      │   │
│  │  │ - description  │  │ - delegate     │  │ - test_skill   │      │   │
│  │  └────────────────┘  └────────────────┘  └────────────────┘      │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                        AGENT POOL                                         │   │
│  │                                                                       │   │
│  │  Agent_1 (Director)                                                   │   │
│  │  ┌─────────────────────────────────────────────────┐                    │   │
│  │  │ id: "uuid-1"                                    │                    │   │
│  │  │ name: "Директор"                                │                    │   │
│  │  │ status: "thinking"                            │                    │   │
│  │  │ model: "llama3.3:70b" @ GPU                  │                    │   │
│  │  │ mcp_tools: [git_manager, task_planner]        │                    │   │
│  │  │ is_director: true                         │                    │   │
│  │  └─────────────────────────────────────────┘                    │   │
│  │                                                                       │   │
│  │  Agent_2 (Worker)                                                    │   │
│  │  ┌─────────────────────────────────────────────────┐                    │   │
│  │  │ id: "uuid-2"                                    │                    │   │
│  │  │ name: "Кодер Вася"                             │                    │   │
│  │  │ status: "idle"                                │                    │   │
│  │  │ model: "codellama:7b" @ CPU                    │                    │   │
│  │  │ mcp_tools: [code_linter]                     │                    │   │
│  │  │ is_director: false                        │                    │   │
│  │  └────────────���─��──────────────────────────┘                    │   │
│  │                                                                       │   │
│  │  Agent_3 (Worker)                                                    │   │
│  │  ┌─────────────────────────────────────────────────┐                    │   │
│  │  │ id: "uuid-3"                                    │                    │   │
│  │  │ name: "Тестировщик"                          │                    │   │
│  │  │ status: "idle"                                │                   │   │
│  │  │ model: "llama3.3:7b" @ CPU                    │                    │   │
│  │  │ mcp_tools: [security_scanner, docker_manager] │   │                    │   │
│  │  │ is_director: false                         │                    │   │
│  │  └─────────────────────────────────────────┘                    │   │
│  │                                                                       │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│  ┌────────────────────────┐  ┌────────────────────────┐                    │
│  │    MODEL REGISTRY       │  │    MCP CLIENT          │                    │
│  │  - HuggingFace API     │  │  - Tool registry     │                    │
│  │  - Model catalog     │  │  - Tool runner      │                    │
│  │  - Download manager │  │  - Result parser   │                    │
│  └────────────────────────┘  └────────────────────────┘                    │
│                                                                              │
│  ┌─────────────────────────────────────────────┐  ┌─────────────────────────┐   │
│  │        RESOURCE MANAGER                    │  │    TASK MANAGER          │   │
│  │  - RAM usage (psutil)                    │  │  - Task queue           │   │
│  │  - VRAM usage (pynvml)                 │  │  - Kanban states       │   │
│  │  - Model hot-swap                      │  │  - Assignment logic   │   │
│  │  - Memory optimization                │  │  - Progress tracking  │   │
│  └────────────────────────────────────────┘  └─────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                     ┌──────────────┴───────────────┐
                     │      OLLAMA SERVERS           │
                     │  ┌─────────┐  ┌─────────┐  │
                     │  │  GPU    │  │   CPU   │  │
                     │  │ :11434 │  │  :11435 │  │
                     │  │ Director│  │ Workers │ │
                     │  │ model  │  │ models  │ │
                     │  └─────────┘  └─────────┘  │
                     └──────────────────────────────┘
```

---

## Модели данных

### Agent (Сотрудник)
```python
class Agent(BaseModel):
    id: UUID
    name: str                        # "Кодер Вася"
    description: str               # "Специалист по Python"
    
    # Модель
    model_name: str               # "llama3.3:latest"
    model_provider: str            # "ollama" | "lm_studio"
    port: int                    # 11434 | 11435
    
    # Статус
    status: AgentStatus           # idle | thinking | working | error
    
    # Роль
    is_director: bool = False     # True = управляет командой
    
    # Навыки (MCP инструменты)
    mcp_tools: List[str] = []    # ["code_linter", "security_scanner"]
    
    # Метрики
    tasks_completed: int = 0
    last_active: datetime = None
```

### AgentProfile (Профиль при найме)
```python
class AgentProfile(BaseModel):
    # Базовые данные
    name: str
    description: str
    
    # Модель
    model_source: ModelSource      # ollama | huggingface
    model_name: str              # "llama3.3:70b"
    estimated_vram: str         # "4GB", "8GB"
    
    # Навыки (доступные MCP)
    available_tools: List[str]    # Все доступные инструменты
    
    # Выбранные навыки
    selected_tools: List[str] = []   # Что подключаем при найме
    
    # Роль
    is_director: bool = False   # Назначить директором?
    
    # Тест
    test_prompt: str = ""       # Тестовый промпт
    test_result: str = ""       # Результат теста
```

### MCPTool (Инструмент)
```python
class MCPTool(BaseModel):
    id: str                     # "code_linter"
    name: str                   # "Python Linter"
    description: str            # "Проверяет стиль кода"
    
    command: str                # "pylint {file}"
    args_schema: dict = {}      # Аргументы
    
    category: str              # code | security | git | docker | test
    enabled: bool = True
```

### Task (Задача для Kanban)
```python
class Task(BaseModel):
    id: UUID
    title: str
    description: str
    
    # Kanban state
    status: TaskStatus          # todo | in_progress | review | done
    
    # Назначение
    assigned_agent: UUID = None
    
    # Результат
    result: str = ""
    logs: List[str] = []
```

---

## API Endpoints

### Agent Management
| Method | Endpoint | Description |
|--------|---------|-------------|
| GET | `/api/agents` | Список всех агентов |
| POST | `/api/agents` | Создать карточку кандидата |
| GET | `/api/agents/{id}` | Профиль агента |
| PUT | `/api/agents/{id}` | Редактировать профиль |
| DELETE | `/api/agents/{id}` | Уволить агента |
| POST | `/api/agents/{id}/hire` | Принять на работу |
| POST | `/api/agents/{id}/fire` | Уволить (освободить ресурсы) |
| POST | `/api/agents/{id}/director` | Назначить директором |

### Catalog
| Method | Endpoint | Description |
|--------|---------|-------------|
| GET | `/api/models` | Каталог моделей (HuggingFace) |
| GET | `/api/models/available` | Модели под текущие ресурсы |
| GET | `/api/mcp-tools` | Доступные инструменты |

### Tasks
| Method | Endpoint | Description |
|--------|---------|-------------|
| GET | `/api/tasks` | Список задач |
| POST | `/api/tasks` | Создать задачу |
| PUT | `/api/tasks/{id}` | Обновить статус |

### WebSocket
| Endpoint | Description |
|----------|-------------|
| `/ws/office` | Обновления офиса (все агенты) |
| `/ws/agent/{id}` | Логи конкретного агента |

---

## Пайплайн разработки

### Week 1-2: Core Foundation
```
Day 1-2:   Setup FastAPI project
           - app/main.py
           - app/models.py (Agent, MCPTool, Task)
           - app/database.py (SQLite)

Day 3-4:   CRUD для агентов
           - POST/GET/PUT/DELETE /agents
           - Profile Builder (имя, описание)

Day 5-6:   Ollama базовая интеграция
           - Подключение к Ollama
           - /generate endpoint

Day 7:     Resource Monitor
           - psutil для RAM
           - pynvml для VRAM
```

### Week 3-4: Agent Profiles и MCP
```
Day 8-9:   Model selection
           - Выбор модели для профиля
           - Template системы

Day 10-11: MCP Client
           - Базовый MCP протокол
           - Tool registry

Day 12-13: Профиль с навыками
           - Выбор MCP инструментов
           - Подключение/отключение

Day 14:     Director система
           - Назначение директора
           - is_director flag
```

### Week 5-6: Coordination
```
Day 15-16: Agent comunicação
           - Director → Worker messaging
           - Task assignment

Day 17-18: Task Manager
           - CRUD задач
           - Kanban states

Day 19-20: Логирование
           - WebSocket логи
           - History

Day 21:     CLI interface
           - Простой CLI для тестов
```

### Week 7-9: Frontend
```
Day 22-24: Next.js setup
           - TailwindCSS
           - WebSocket hooks

Day 25-27: Office UI
           - 2D Grid визуализация
           - Agent cards

Day 28-30: Hire/Profile pages
           - Карточка кандидата
           - Редактирование профиля

Day 31-32: Kanban board
           - Drag-n-drop
           - Task states

Day 33:     Real-time updates
           - WebSocket integration
           - Status updates
```

### Week 10-11: Polish
```
Day 34-36: Анимации и UI
           - Agent states animation
           - Terminal emulation

Day 37-38: Testing
           - Integration tests
           - Bug fixes

Day 39-40: Docker + Docs
           - docker-compose.yml
           - README final

Day 41-42: Release
           - GitHub release
           - Demo video
```

---

## Поток данных

### Сценарий: Найм нового агента

```
1. Пользователь открывает страницу "Найм"
2. Заполняет форму:
   - Имя: "Кодер Вася"
   - Модель: выбирает из списка
   - MCP Tools: отмечает галочки
   - Директор: ставит галочку (если нужно)
3. Нажимает "Нанять"
4. Backend:
   - Создаёт Agent в БД
   - Если director=true: обновляет old director
   - Загружает модель в память
5. Frontend:
   - Добавляет карточку в офис
   - Начинает стримить логи
```

### Сценарий: Выполнение задачи

```
1. Пользователь создаёт задачу в Kanban
2. Director (Agent с is_director=True) получает задачу
3. Director:
   - Анализирует задачу
   - Находит свободного Worker
   - Делегирует задачу
4. Worker:
   - Выполняет подзадачу
   - Логирует в WebSocket
   - Возвращает результат
5. Director:
   - Проверяет результат
   - Обновляет Kanban
```

---

## Директор vs Работник

### Director (Управляющий)
- **Один** на команду
- Назначается через галочку "Директор"
- Получает задачи от пользователя
- Координирует других агентов
- Обычно работает на GPU (большая модель)
- Следит за состоянием команды

### Worker (Работник)
- **Много** в команде
- Выполняет конкретные задачи
- Может работать на CPU (легкие модели)
- Имеет свои MCP навыки
- Отчитывается Directorу

---

## Ресурс management

### Распределение моделей

```
GPU (8GB VRAM):
├─ Director: llama3.3:70b (4GB)
└─ Workers: [  ]  // если есть место

CPU (16GB RAM):
├─ Worker 1: codellama:7b (4GB)
├─ Worker 2: llama3.3:7b (4GB)
└─ Worker 3: [waiting]
```

### Hot-swap при нехватке

```
1. Resource Monitor видит OOM
2. Отправляет сигнал Director
3. Director:
   - Останавливает least active Worker
   - Освобождает память
   - Запускает новую задачу
4. Worker перезапускается на другой модели
