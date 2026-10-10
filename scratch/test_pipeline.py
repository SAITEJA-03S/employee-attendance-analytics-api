from pymongo import MongoClient
from datetime import datetime, timedelta

client = MongoClient('mongodb://localhost:27017', tz_aware=True)
db = client['attendance_db']

from_date = '2026-07-01'
to_date = '2026-07-07'
department = 'Engineering'

from_dt = datetime.strptime(from_date, '%Y-%m-%d')
to_dt = datetime.strptime(to_date, '%Y-%m-%d')
to_dt_plus_1 = to_dt + timedelta(days=1)

pipeline = [
    {"$match": {"department": department}},
    {"$limit": 1},
    {"$project": {
        "_id": 0,
        "date_str": {"$literal": from_date}
    }},
    {"$addFields": {
        "date_dt": {"$dateFromString": {"dateString": "$date_str", "format": "%Y-%m-%d"}}
    }},
    {"$densify": {
        "field": "date_dt",
        "range": {
            "step": 1,
            "unit": "day",
            "bounds": [from_dt, to_dt_plus_1]
        }
    }},
    {"$addFields": {
        "date": {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}},
        "is_working_day": {"$lt": [{"$dayOfWeek": "$date_dt"}, 7]}
    }},
    {
        "$lookup": {
            "from": "employees",
            "let": {"d_str": "$date"},
            "pipeline": [
                {
                    "$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$department", department]},
                                {"$lte": ["$joined_on", "$$d_str"]}
                            ]
                        }
                    }
                },
                {"$count": "count"}
            ],
            "as": "hc_info"
        }
    },
    {
        "$addFields": {
            "headcount": {
                "$ifNull": [{"$arrayElemAt": ["$hc_info.count", 0]}, 0]
            }
        }
    },
    {
        "$lookup": {
            "from": "attendance_logs",
            "let": {"d_str": "$date"},
            "pipeline": [
                {
                    "$match": {
                        "$expr": {
                            "$eq": ["$date", "$$d_str"]
                        }
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "emp"
                    }
                },
                {"$unwind": "$emp"},
                {
                    "$match": {
                        "emp.department": department
                    }
                }
            ],
            "as": "day_logs"
        }
    },
    {
        "$addFields": {
            "present_count": {
                "$reduce": {
                    "input": "$day_logs",
                    "initialValue": 0.0,
                    "in": {
                        "$add": [
                            "$$value",
                            {
                                "$cond": [
                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": ["$$this.half_day", 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        ]
                    }
                }
            },
            "late_count": {
                "$reduce": {
                    "input": "$day_logs",
                    "initialValue": 0,
                    "in": {
                        "$add": [
                            "$$value",
                            {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]},
                                    1,
                                    0
                                ]
                            }
                        ]
                    }
                }
            }
        }
    },
    {
        "$addFields": {
            "is_mon_fri": {
                "$and": [
                    {"$gte": [{"$dayOfWeek": "$date_dt"}, 2]},
                    {"$lte": [{"$dayOfWeek": "$date_dt"}, 6]}
                ]
            }
        }
    },
    {
        "$addFields": {
            "attendance_rate": {
                "$cond": [
                    {
                        "$and": [
                            "$is_mon_fri",
                            {"$gt": ["$headcount", 0]}
                        ]
                    },
                    {
                        "$divide": ["$present_count", "$headcount"]
                    },
                    None
                ]
            }
        }
    },
    {
        "$setWindowFields": {
            "sortBy": {"date_dt": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, 0]
                    }
                }
            }
        }
    },
    {
        "$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": "$is_mon_fri",
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1
        }
    },
    {"$sort": {"date": 1}}
]

res = list(db.employees.aggregate(pipeline))
print("Pipeline output count:", len(res))
for row in res:
    print(row)
