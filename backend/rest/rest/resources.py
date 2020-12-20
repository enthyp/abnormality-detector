from dateutil.parser import parse, ParserError
from flask_cors import cross_origin
from flask_restful import Resource, Api
from flask import current_app, request
from marshmallow import ValidationError
import pandas as pd
import pytz
import requests

from .db import dao
from .bridge import dao as bridge_dao
from .exceptions import NotFoundError
from .kafka import kafka_consumer
from .reports import RoomReportGenerator, MeetingReportGenerator
from .schema import (
    anomaly_schema,
    meeting_schema,
    meeting_request_schema,
    report_request_schema,
)


class Meetings(Resource):
    @cross_origin()
    def get(self):
        return {"meetings": bridge_dao.get_meetings()}

    @cross_origin()
    def put(self):
        json_data = request.get_json()
        if not json_data:
            return {"message": "No input data provided"}, 400
        try:
            data = meeting_request_schema.load(json_data)
        except ValidationError as err:
            return err.messages, 422

        dao.update_meeting(data["name"], data["criteria"])
        return {"message": f"successfully updated {data['name']}"}, 200

    @cross_origin()
    def delete(self):
        name = request.args.get("name", None)
        if not name:
            return {"message": f"conference to remove is not specified"}, 400

        try:
            enc_name = requests.utils.quote(name)
            r_thresh = requests.delete(f'http://backend-streaming:5000/monitoring/{enc_name}')
            r_ml = requests.delete(f'http://backend-streaming:5000/anomaly-detection/{enc_name}')
            
            if r_thresh.status_code != 200 or r_ml.status_code != 200:
                current_app.logger.warning(f'monitoring: {r_thresh.status_code}, anomaly-detection: {r_ml.status_code}')
                current_app.logger.warning(f'monitoring: {r_thresh.content}, anomaly-detection: {r_ml.content}')
                return {'message': f'failed to unschedule monitoring and inference for meeting {name}'}, 400

            dao.clear_meeting(name)
            return {
                "message": f"successfully removed {name} from monitored conferences"
            }, 200
        except NotFoundError:
            return {"message": f"no meeting {name}"}, 404


class MeetingDetails(Resource):
    @cross_origin()
    def get(self, meeting_name):
        if meeting := dao.meeting_details(meeting_name):
            return meeting_schema.dump(meeting)
        else:
            return {"message": f"no meeting {meeting_name}"}, 404


class Calls(Resource):
    @cross_origin()
    def get(self):
        result = dao.get_conferences()
        result["created"] = list(map(meeting_schema.dump, result["created"]))
        return result


class CallHistory(Resource):
    @cross_origin()
    def get(self, meeting_name):
        start_date = request.args.get("start", None)
        end_date = request.args.get("end", None)
        min_duration = request.args.get("min_duration", None)
        max_participants = request.args.get("max_participants", None)

        try:
            # TODO: timezone!
            if start_date:
                start_date = parse(start_date)
            if end_date:
                end_date = parse(end_date)
        except ParserError:
            return {"message": "invalid date format"}, 400

        result = dao.get_calls(
            meeting_name, start_date, end_date, min_duration, max_participants
        )
        return {"calls": result}


class Anomalies(Resource):
    @cross_origin()
    def get(self, meeting_name):
        start_date = request.args.get("start", None)
        end_date = request.args.get("end", None)

        try:
            # TODO: timezone!
            if start_date:
                start_date = parse(start_date)
            if end_date:
                end_date = parse(end_date)
        except ParserError:
            return {"message": "invalid date format"}, 400

        result = dao.get_anomalies(meeting_name, start_date, end_date)
        result["anomalies"] = list(map(anomaly_schema.dump, result["anomalies"]))
        for res in result['anomalies']:
            ml_score, ml_thresh = res.pop('ml_anomaly_score'), res.pop('ml_threshold')
            res['anomaly_reason'].append(
                {
                    'parameter': 'ML model', 
                    'value': ml_score, 
                    'condition_type': 'probability[%]', 
                    'condition_value': ml_thresh
                }
            )
        return result


class Report(Resource):
    @cross_origin()
    def get(self, meeting_name):
        if errors := report_request_schema.validate(request.args.to_dict()):
            return {"message": f"invalid request: {errors}"}, 400

        try:
            start_datetime = (
                pd.Timestamp(request.args["start_datetime"]).tz_convert(pytz.timezone("Europe/Warsaw"))
                if "start_datetime" in request.args
                else None
            )
        except ParserError:
            return {"message": f"invalid datetime format"}, 400

        try:
            raport_generator = (
                MeetingReportGenerator(dao, meeting_name, start_datetime)
                if start_datetime
                else RoomReportGenerator(dao, meeting_name)
            )
            pdf_file = raport_generator.generate_pdf_report()
        except NotFoundError:
            return {
                "message": f"No data found for given conditions, cannot create a report"
            }, 400

        return (
            pdf_file,
            200,
            {
                "Content-Type": "application/pdf",
                "Content-Disposition": 'inline; filename="report.pdf"',
            },
        )


class Models(Resource):
    @cross_origin()
    def get(self, meeting_name):
        result = dao.get_last_training(meeting_name)
        return {'last': result}


class Notifications(Resource):
    @cross_origin()
    def get(self):
        try:
            num_msgs = request.args.get("count")
            result = kafka_consumer.get_last(num_msgs)
            return {'last': result}
        except KeyError:
            return {'message': '"count" is mandatory'}, 400


def setup_resources(app):
    api = Api(app)
    api.add_resource(Report, "/reports/<string:meeting_name>")
    api.add_resource(Meetings, "/meetings")
    api.add_resource(MeetingDetails, "/meetings/<string:meeting_name>")
    api.add_resource(Calls, "/calls")
    api.add_resource(CallHistory, "/calls/<string:meeting_name>")
    api.add_resource()
    api.add_resource(Anomalies, "/anomalies/<string:meeting_name>")
    api.add_resource(Models, "/models/<string:meeting_name>")

