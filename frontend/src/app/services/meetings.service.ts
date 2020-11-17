import {Injectable} from '@angular/core';
import {HttpClient} from '@angular/common/http';
import {environment} from '../../environments/environment';
import {Observable} from 'rxjs';
import {AllMeetings} from '../components/meetings/class/all-meetings';
import {Meeting} from '../components/meetings/class/meeting';

@Injectable({
  providedIn: 'root'
})
export class MeetingsService {


  private backend = environment.backend.rest;

  constructor(private http: HttpClient) {
  }

  put_meeting(meeting: Meeting): Observable<any> {
    const url = `${this.backend.url}/${this.backend.meetings}`;
    console.log(meeting);
    return this.http.put(url, meeting);
  }

  fetch_criteria(name: string): Observable<any> {
    const url = `${this.backend.url}/${this.backend.meetings}/${name}`;
    return this.http.get<any>(url);
  }

  fetch_meetings(): Observable<AllMeetings> {
    const url = `${this.backend.url}/${this.backend.meetings}`;
    return this.http.get<AllMeetings>(url);
  }

}
